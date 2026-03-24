from __future__ import annotations

import sys
from pathlib import Path

import pytest

from config import load_config
from service import OrchestratorService


@pytest.mark.asyncio
async def test_service_start_and_read_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "fake-server-state.json"
    monkeypatch.setenv("FAKE_APP_SERVER_STATE_PATH", str(state_path))
    config = load_config(
        data_dir=tmp_path / "registry",
        server_cmd=[sys.executable, "-m", "tests.fake_app_server"],
    )
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
async def test_autosteer_uses_active_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "fake-server-state.json"
    monkeypatch.setenv("FAKE_APP_SERVER_STATE_PATH", str(state_path))
    config = load_config(
        data_dir=tmp_path / "registry",
        server_cmd=[sys.executable, "-m", "tests.fake_app_server"],
    )
    service = OrchestratorService(config)
    try:
        await service.connect()
        result = await service.start_new_thread("hold position")
        queued = service.queue_input(result.thread.thread_id, "finish this up", mode="auto")
        processed = await service.process_queue(result.thread.thread_id)
        updated_queue = service.registry.get_queued_input(queued.id)
        turns = service.registry.list_turns(thread_id=result.thread.thread_id)

        assert processed
        assert updated_queue is not None
        assert updated_queue.status == "done"
        assert updated_queue.action_taken == "steer"
        assert any(turn.status == "completed" for turn in turns)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resume_thread_handles_large_jsonl_messages(tmp_path: Path) -> None:
    config = load_config(
        data_dir=tmp_path / "registry",
        server_cmd=[sys.executable, "-m", "tests.huge_line_server"],
    )
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
