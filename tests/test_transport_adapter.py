from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from config import load_config
from service import OrchestratorService
from transport import StdioJsonRpcTransport, UNHANDLED


def _write_config(tmp_path: Path, server_command: list[str], *, data_dir: str = "registry") -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                f'command = ["{server_command[0]}", "{server_command[1]}", "{server_command[2]}"]',
                "",
                "[registry]",
                f'data_dir = "{(tmp_path / data_dir).as_posix()}"',
                "",
                "[logging]",
                'level = "INFO"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def _write_turn_options_config(tmp_path: Path, server_command: list[str]) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                f'command = ["{server_command[0]}", "{server_command[1]}", "{server_command[2]}"]',
                "",
                "[registry]",
                f'data_dir = "{(tmp_path / "registry").as_posix()}"',
                "",
                "[turn_start_options]",
                'sandbox_mode = "danger-full-access"',
                'approval_policy = "never"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.mark.asyncio
async def test_service_start_and_read_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "fake-server-state.json"
    monkeypatch.setenv("FAKE_APP_SERVER_STATE_PATH", str(state_path))
    config = load_config(_write_config(tmp_path, [sys.executable, "-m", "tests.fake_app_server"]))
    service = OrchestratorService(config)
    try:
        await service.connect()
        result = await service.start_new_thread("run tests")
        reread = await service.read_thread(result.thread.thread_id, include_turns=True)
        turns = service.registry.list_turns(thread_id=result.thread.thread_id)

        assert reread.thread_id == result.thread.thread_id
        assert turns
        assert turns[0].status in {"completed", "inProgress"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_lists_threads_after_seeded_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "fake-server-state.json"
    monkeypatch.setenv("FAKE_APP_SERVER_STATE_PATH", str(state_path))
    config = load_config(_write_config(tmp_path, [sys.executable, "-m", "tests.fake_app_server"]))
    service = OrchestratorService(config)
    try:
        await service.connect()
        result = await service.start_new_thread("hold position")
        rows = await service.list_threads()

        assert any(row.thread_id == result.thread.thread_id for row in rows)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_thread_handles_large_jsonl_messages(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, [sys.executable, "-m", "tests.huge_line_server"]))
    service = OrchestratorService(config)
    try:
        await service.connect()
        thread = await service.resume_thread("thr_large")
        assert thread.thread_id == "thr_large"
        assert thread.cwd == "/tmp/huge-thread"
        assert thread.preview is not None
        assert len(thread.preview) == 200_000
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_start_turn_on_existing_thread_uses_configured_turn_start_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "fake-server-state.json"
    monkeypatch.setenv("FAKE_APP_SERVER_STATE_PATH", str(state_path))
    config = load_config(_write_turn_options_config(tmp_path, [sys.executable, "-m", "tests.fake_app_server"]))
    service = OrchestratorService(config)
    try:
        await service.connect()
        result = await service.start_new_thread("completed baseline")
        turn = await service.start_turn_on_thread(result.thread.thread_id, "new follow up")

        assert turn.turn_id
        stored_state = json.loads(state_path.read_text(encoding="utf-8"))
        latest_turn = stored_state["turns"][turn.turn_id]
        assert latest_turn["sandbox_mode"] == "danger-full-access"
        assert latest_turn["approval_policy"] == "never"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_transport_logs_unhandled_jsonrpc_message_without_crashing(caplog: pytest.LogCaptureFixture) -> None:
    transport = StdioJsonRpcTransport(["python3", "-c", "pass"])
    sent_payloads: list[dict[str, object]] = []

    async def fake_send(payload: dict[str, object]) -> None:
        sent_payloads.append(payload)

    transport._send = fake_send  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        await transport._handle_message({"id": 1, "method": "unexpected/mixed", "params": {"value": 1}})
        await asyncio.sleep(0)

    assert "Unhandled JSON-RPC server request" in caplog.text
    assert any(getattr(record, "jsonrpc_message", None) == {"id": 1, "method": "unexpected/mixed", "params": {"value": 1}} for record in caplog.records)
    assert sent_payloads == [{"id": 1, "error": {"code": -32601, "message": "No client handler for unexpected/mixed"}}]


@pytest.mark.asyncio
async def test_transport_replies_to_server_requests_via_request_handler() -> None:
    transport = StdioJsonRpcTransport(["python3", "-c", "pass"])
    sent_payloads: list[dict[str, object]] = []

    async def fake_send(payload: dict[str, object]) -> None:
        sent_payloads.append(payload)

    def handler(method: str, params: dict[str, object]) -> object:
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept"}
        return UNHANDLED

    transport.add_request_handler(handler)
    transport._send = fake_send  # type: ignore[method-assign]

    await transport._handle_message({"id": 7, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thr_1"}})
    await asyncio.sleep(0)

    assert sent_payloads == [{"id": 7, "result": {"decision": "accept"}}]
