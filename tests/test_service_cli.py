from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cli import (
    _extract_live_message_entry,
    _extract_message_entries_from_payload,
    app,
)
from models import EventRecord


def test_cli_start_threads_and_status(runner: CliRunner, fake_server_env: dict[str, str], tmp_path: Path) -> None:
    result = runner.invoke(app, ["start", "ship it"], env=fake_server_env)
    assert result.exit_code == 0
    assert "Started thread" in result.stdout

    list_result = runner.invoke(app, ["threads"], env=fake_server_env)
    assert list_result.exit_code == 0
    assert "Threads" in list_result.stdout
    assert "Core" in list_result.stdout
    assert "Folder" in list_result.stdout
    assert "Last Turn" in list_result.stdout

    status_result = runner.invoke(app, ["status"], env=fake_server_env)
    assert status_result.exit_code == 0
    assert "Queued inputs:" in status_result.stdout


def test_cli_queue_and_autosteer(runner: CliRunner, fake_server_env: dict[str, str]) -> None:
    start_result = runner.invoke(app, ["start", "hold the turn"], env=fake_server_env)
    assert start_result.exit_code == 0
    thread_id = start_result.stdout.split()[2]

    queue_result = runner.invoke(app, ["queue", thread_id, "finish now"], env=fake_server_env)
    assert queue_result.exit_code == 0
    assert "Queued" in queue_result.stdout

    autosteer_result = runner.invoke(app, ["autosteer", thread_id], env=fake_server_env)
    assert autosteer_result.exit_code == 0
    assert "Autosteer" in autosteer_result.stdout


def test_cli_listen_streams_thread_events_to_console(runner: CliRunner, fake_server_env: dict[str, str]) -> None:
    start_result = runner.invoke(app, ["start", "hold the turn"], env=fake_server_env)
    assert start_result.exit_code == 0
    thread_id = start_result.stdout.split()[2]

    listen_result = runner.invoke(app, ["listen", thread_id, "--max-events", "1"], env=fake_server_env)
    assert listen_result.exit_code == 0
    assert f"Listening on thread {thread_id}" in listen_result.stdout
    assert "user: hold the turn" in listen_result.stdout


def test_cli_listen_history_limit_does_not_replay_full_backlog_on_resume(
    runner: CliRunner,
    fake_server_env: dict[str, str],
) -> None:
    start_result = runner.invoke(app, ["start", "first prompt"], env=fake_server_env)
    assert start_result.exit_code == 0
    thread_id = start_result.stdout.split()[2]

    continue_result = runner.invoke(app, ["continue", thread_id, "second prompt"], env=fake_server_env)
    assert continue_result.exit_code == 0

    listen_result = runner.invoke(
        app,
        ["listen", thread_id, "--history-limit", "2", "--max-events", "1"],
        env=fake_server_env,
    )
    assert listen_result.exit_code == 0
    assert "user: second prompt" in listen_result.stdout
    assert "assistant/commentary: working on: second prompt" in listen_result.stdout
    assert "user: first prompt" not in listen_result.stdout


def test_extract_message_entries_from_thread_read_payload_keeps_latest_messages() -> None:
    entries = _extract_message_entries_from_payload(
        {
            "id": "turn_latest",
            "items": [
                {
                    "id": "item_user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "latest user prompt"}],
                },
                {
                    "id": "item_agent",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "latest assistant reply",
                },
            ],
        }
    )

    assert entries == [
        ("turn_latest:item_user:user", "user: latest user prompt"),
        ("turn_latest:item_agent:assistant", "assistant/commentary: latest assistant reply"),
    ]


def test_extract_live_message_entry_from_agent_delta() -> None:
    event = EventRecord(
        id="evt_1",
        thread_id="thr_1",
        turn_id="turn_1",
        event_type="item/agentMessage/delta",
        payload_json={"threadId": "thr_1", "turnId": "turn_1", "delta": "streaming text"},
    )

    assert _extract_live_message_entry(event) == ("live:evt_1", "assistant/live: streaming text")
