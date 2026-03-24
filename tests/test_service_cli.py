from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from cli import (
    _extract_live_message_entry,
    _extract_message_entries_from_payload,
    app,
)
from models import EventRecord


def _seed_thread(fake_server_setup: dict[str, object], *, prompts: list[str], active_last: bool = False) -> str:
    state_path = Path(str(fake_server_setup["state_path"]))
    thread_id = "thr_seeded"
    state = {
        "threads": {},
        "turns": {},
        "loaded": [],
        "counter": len(prompts) * 10 + 1,
    }
    turn_ids: list[str] = []
    for index, prompt in enumerate(prompts, start=1):
        turn_id = f"turn_{index}"
        turn_ids.append(turn_id)
        is_active_turn = active_last and index == len(prompts)
        state["turns"][turn_id] = {
            "id": turn_id,
            "threadId": thread_id,
            "status": "inProgress" if is_active_turn else "completed",
            "items": [
                {
                    "id": f"item_{index}_user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": prompt}],
                }
            ],
            "error": None,
            "summary": None if is_active_turn else f"Completed: {prompt}",
        }
        if not is_active_turn:
            state["turns"][turn_id]["items"].append(
                {
                    "id": f"item_{index}_agent",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": f"working on: {prompt}",
                }
            )
    state["threads"][thread_id] = {
        "id": thread_id,
        "name": "Seeded Thread",
        "preview": prompts[-1],
        "cwd": "/tmp/core-folder",
        "createdAt": 1,
        "updatedAt": len(prompts) + 1,
        "archived": False,
        "status": {"type": "active"} if active_last else {"type": "idle"},
        "activeTurnId": turn_ids[-1] if active_last else None,
        "turns": turn_ids,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return thread_id


def test_cli_threads_and_doctor(runner: CliRunner, fake_server_setup: dict[str, object]) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["ship it"])
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    list_result = runner.invoke(app, ["threads", "--config", config_path], env=env)
    assert list_result.exit_code == 0
    assert "Threads" in list_result.stdout
    assert "Seeded" in list_result.stdout
    assert "Core" in list_result.stdout
    assert "Folder" in list_result.stdout
    assert "Last Turn" in list_result.stdout

    doctor_result = runner.invoke(app, ["doctor", "--config", config_path], env=env)
    assert doctor_result.exit_code == 0
    assert "Doctor" in doctor_result.stdout
    assert "connect" in doctor_result.stdout


def test_cli_read_and_inspect_thread(runner: CliRunner, fake_server_setup: dict[str, object]) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["inspect me"])
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    read_result = runner.invoke(app, ["read", thread_id, "--config", config_path], env=env)
    assert read_result.exit_code == 0
    assert "Threads" in read_result.stdout
    assert "Turns" in read_result.stdout
    assert "Seeded" in read_result.stdout

    inspect_result = runner.invoke(app, ["inspect", thread_id, "--config", config_path], env=env)
    assert inspect_result.exit_code == 0
    assert f"Thread: {thread_id}" in inspect_result.stdout
    assert "Turns" in inspect_result.stdout


def test_cli_listen_streams_thread_messages_to_console(runner: CliRunner, fake_server_setup: dict[str, object]) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["hold the turn"])
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    listen_result = runner.invoke(app, ["listen", thread_id, "--max-events", "1", "--config", config_path], env=env)
    assert listen_result.exit_code == 0
    assert f"Listening on thread {thread_id}" in listen_result.stdout
    assert "user: hold the turn" in listen_result.stdout


def test_cli_listen_history_limit_does_not_replay_full_backlog_on_resume(
    runner: CliRunner,
    fake_server_setup: dict[str, object],
) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["first prompt", "second prompt"])
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    listen_result = runner.invoke(
        app,
        ["listen", thread_id, "--history-limit", "2", "--max-events", "1", "--config", config_path],
        env=env,
    )
    assert listen_result.exit_code == 0
    assert "user: second prompt" in listen_result.stdout
    assert "assistant/commentary: working on: second prompt" in listen_result.stdout
    assert "user: first prompt" not in listen_result.stdout


def test_cli_listen_and_send_starts_new_turn_even_when_thread_is_active(
    runner: CliRunner,
    fake_server_setup: dict[str, object],
) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["hold the turn"], active_last=True)
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    result = runner.invoke(
        app,
        ["listen-and-send", thread_id, "--max-events", "2", "--config", config_path],
        env=env,
        input="finish now\n",
    )
    assert result.exit_code == 0
    assert f"Listening and sending on thread {thread_id}" in result.stdout
    assert "user: hold the turn" in result.stdout
    assert "Started turn" in result.stdout
    assert result.stdout.count("user: finish now") == 1
    assert "assistant/commentary: working on: finish now" in result.stdout


def test_cli_listen_and_send_starts_next_turn_when_thread_is_idle(
    runner: CliRunner,
    fake_server_setup: dict[str, object],
) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["completed prompt"], active_last=False)
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    result = runner.invoke(
        app,
        ["listen-and-send", thread_id, "--max-events", "2", "--config", config_path],
        env=env,
        input="new follow up\n",
    )
    assert result.exit_code == 0
    assert f"Listening and sending on thread {thread_id}" in result.stdout
    assert "user: completed prompt" in result.stdout
    assert "Started turn" in result.stdout
    assert result.stdout.count("user: new follow up") == 1
    assert "assistant/commentary: working on: new follow up" in result.stdout


def test_cli_listen_and_send_handles_command_approval_requests(
    runner: CliRunner,
    fake_server_setup: dict[str, object],
) -> None:
    thread_id = _seed_thread(fake_server_setup, prompts=["completed prompt"], active_last=False)
    config_path = str(fake_server_setup["config_path"])
    env = dict(fake_server_setup["env"])

    result = runner.invoke(
        app,
        ["listen-and-send", thread_id, "--max-events", "6", "--config", config_path],
        env=env,
        input="git commit\napprove\n",
    )
    assert result.exit_code == 0
    assert "approval: Approve command execution?" in result.stdout
    assert "reply with: approve, cancel" in result.stdout
    assert "approval sent: accept" in result.stdout
    assert "assistant/commentary: approved: git commit" in result.stdout


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
        ("turn_latest:user:latest user prompt", "user: latest user prompt"),
        ("turn_latest:assistant:latest assistant reply", "assistant/commentary: latest assistant reply"),
    ]


def test_extract_live_message_entry_from_agent_delta() -> None:
    event = EventRecord(
        id="evt_1",
        thread_id="thr_1",
        turn_id="turn_1",
        event_type="item/agentMessage/delta",
        payload_json={"threadId": "thr_1", "turnId": "turn_1", "delta": "streaming text"},
    )

    assert _extract_live_message_entry(event) is None
