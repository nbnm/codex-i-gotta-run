from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from models import TelegramConfig, TelegramSessionRecord
from registry import JsonRegistry
from telegram_integration import TelegramOperatorBridge, build_topic_name, format_telegram_text


def _write_telegram_config(tmp_path: Path, *, default_chat_id: int | None = 777) -> tuple[Path, Path]:
    state_path = tmp_path / "fake-server-state.json"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[server]",
                f'command = ["{sys.executable}", "-m", "tests.fake_app_server"]',
                "",
                "[registry]",
                f'data_dir = "{(tmp_path / "registry").as_posix()}"',
                "",
                "[telegram]",
                'telegram_bot_token_env = "TEST_TELEGRAM_BOT_TOKEN"',
                'telegram_allowed_chat_ids_env = "TEST_TELEGRAM_ALLOWED_CHAT_IDS"',
                'telegram_bot_allow_username = "TEST_TELEGRAM_BOT_ALLOW_USERNAME"',
                *([f'telegram_default_chat_id_env = "TEST_TELEGRAM_DEFAULT_CHAT_ID"', ""] if default_chat_id is not None else []),
                "",
            ]
        ),
        encoding="utf-8",
    )
    if default_chat_id is not None:
        os.environ["TEST_TELEGRAM_DEFAULT_CHAT_ID"] = str(default_chat_id)
    os.environ["TEST_TELEGRAM_ALLOWED_CHAT_IDS"] = "777"
    return config_path, state_path


def _seed_thread(state_path: Path, *, prompt: str) -> str:
    thread_id = "thr_seeded"
    state = {
        "threads": {
            thread_id: {
                "id": thread_id,
                "name": "Seeded Thread",
                "preview": prompt,
                "cwd": "/tmp/core-folder",
                "createdAt": 1,
                "updatedAt": 2,
                "archived": False,
                "status": {"type": "idle"},
                "activeTurnId": None,
                "turns": ["turn_1"],
            }
        },
        "turns": {
            "turn_1": {
                "id": "turn_1",
                "threadId": thread_id,
                "status": "completed",
                "items": [
                    {"id": "item_user", "type": "userMessage", "content": [{"type": "text", "text": prompt}]},
                    {"id": "item_agent", "type": "agentMessage", "phase": "commentary", "text": f"working on: {prompt}"},
                ],
                "error": None,
                "summary": f"Completed: {prompt}",
            }
        },
        "loaded": [],
        "counter": 100,
        "pending_approvals": {},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return thread_id


class FakeTelegramApi:
    def __init__(self, updates: list[list[dict[str, object]]] | None = None) -> None:
        self._updates = list(updates or [])
        self.sent_messages: list[dict[str, object]] = []
        self.created_topics: list[dict[str, object]] = []
        self.deleted_topics: list[dict[str, object]] = []
        self.closed = False

    async def create_forum_topic(self, chat_id: int, name: str) -> dict[str, object]:
        topic = {
            "name": name,
            "message_thread_id": 1000 + len(self.created_topics) + 1,
            "chat_id": chat_id,
        }
        self.created_topics.append(topic)
        return topic

    async def delete_forum_topic(self, chat_id: int, message_thread_id: int) -> None:
        self.deleted_topics.append({"chat_id": chat_id, "message_thread_id": message_thread_id})

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, object]]:
        if self._updates:
            return self._updates.pop(0)
        await asyncio.sleep(3600)
        return []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "message_thread_id": message_thread_id,
                "reply_markup": reply_markup,
            }
        )

    async def close(self) -> None:
        self.closed = True


def _telegram_env(state_path: Path) -> dict[str, str]:
    env = {
        "FAKE_APP_SERVER_STATE_PATH": str(state_path),
        "TEST_TELEGRAM_BOT_TOKEN": "test-token",
        "TEST_TELEGRAM_BOT_ALLOW_USERNAME": "@oleg",
        "TEST_TELEGRAM_ALLOWED_CHAT_IDS": "777",
    }
    current_chat_id = os.environ.get("TEST_TELEGRAM_DEFAULT_CHAT_ID")
    if current_chat_id is not None:
        env["TEST_TELEGRAM_DEFAULT_CHAT_ID"] = current_chat_id
    return env


@pytest.mark.asyncio
async def test_telegram_bridge_binds_authorized_chat_and_flushes_buffer(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg", default_chat_id=777)
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 10,
                    "message": {
                        "message_id": 1,
                        "text": "new follow up",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )
    bridge = TelegramOperatorBridge(thread_id="thr_1", registry=registry, config=config, api=api)

    await bridge.send_text("buffered before attach")
    await bridge.start()
    inbound = await asyncio.wait_for(bridge.read_input(), timeout=1)
    await bridge.send_text("threaded follow up")
    session = registry.get_telegram_session("thr_1")
    await bridge.close()

    assert inbound == "new follow up"
    assert session is not None
    assert session.chat_id == 777
    assert session.message_thread_id == 321
    assert session.last_update_id == 10
    assert api.sent_messages[0]["chat_id"] == 777
    assert api.sent_messages[0]["text"] == "buffered before attach"
    assert api.sent_messages[0]["parse_mode"] == "HTML"
    assert api.sent_messages[0]["message_thread_id"] is None
    assert api.sent_messages[-1]["message_thread_id"] == 321
    assert api.closed is True


@pytest.mark.asyncio
async def test_telegram_bridge_can_bind_without_default_chat_id(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg")
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 15,
                    "message": {
                        "message_id": 1,
                        "text": "attach me",
                        "chat": {"id": 777, "type": "private"},
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )
    bridge = TelegramOperatorBridge(thread_id="thr_bind", registry=registry, config=config, api=api)

    await bridge.send_text("buffered before bind")
    await bridge.start()
    inbound = await asyncio.wait_for(bridge.read_input(), timeout=1)
    session = registry.get_telegram_session("thr_bind")
    await bridge.close()

    assert inbound == "attach me"
    assert session is not None
    assert session.chat_id == 777
    assert api.sent_messages[0]["text"] == "buffered before bind"
    assert api.sent_messages[1]["text"] == (
        "Attached to thread thr_bind. Send a message to start the next turn. "
        "Use approve or cancel when a command approval is requested."
    )


@pytest.mark.asyncio
async def test_telegram_bridge_ignores_unauthorized_messages(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg", default_chat_id=777)
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 11,
                    "message": {
                        "message_id": 1,
                        "text": "blocked",
                        "chat": {"id": 999, "type": "private"},
                        "from": {"username": "mallory"},
                    },
                }
            ]
        ]
    )
    bridge = TelegramOperatorBridge(thread_id="thr_2", registry=registry, config=config, api=api)

    await bridge.start()
    await asyncio.sleep(0.05)
    await bridge.close()

    session = registry.get_telegram_session("thr_2")

    assert session is not None
    assert session.chat_id == 777
    assert session.message_thread_id is None
    assert len(api.sent_messages) == 1
    assert api.sent_messages[0]["message_thread_id"] is None


@pytest.mark.asyncio
async def test_telegram_bridge_reuses_private_thread_id_after_first_threaded_message(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(TelegramSessionRecord(thread_id="thr_3", chat_id=777))
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg", default_chat_id=777)
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 12,
                    "message": {
                        "message_id": 1,
                        "text": "first threaded message",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )

    bridge = TelegramOperatorBridge(thread_id="thr_3", registry=registry, config=config, api=api)
    await bridge.start()
    inbound = await asyncio.wait_for(bridge.read_input(), timeout=1)
    await bridge.send_text("reply in thread")
    session = registry.get_telegram_session("thr_3")
    await bridge.close()

    assert inbound == "first threaded message"
    assert session is not None
    assert session.message_thread_id == 321
    assert api.sent_messages[-1]["message_thread_id"] == 321


@pytest.mark.asyncio
async def test_telegram_bridge_ignores_messages_from_other_private_threads(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg", default_chat_id=777)
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 13,
                    "message": {
                        "message_id": 1,
                        "text": "first threaded message",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                },
                {
                    "update_id": 14,
                    "message": {
                        "message_id": 2,
                        "text": "wrong thread",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 999,
                        "from": {"username": "oleg"},
                    },
                },
            ]
        ]
    )

    bridge = TelegramOperatorBridge(thread_id="thr_4", registry=registry, config=config, api=api)
    await bridge.start()
    inbound = await asyncio.wait_for(bridge.read_input(), timeout=1)
    await asyncio.sleep(0.05)
    await bridge.close()

    assert inbound == "first threaded message"


def test_format_telegram_text_bolds_prefix() -> None:
    assert format_telegram_text("assistant/commentary: hello <world>") == "<b>assistant/commentary</b>: hello &lt;world&gt;"


def test_format_telegram_text_mentions_username_for_approval_and_final_messages() -> None:
    assert format_telegram_text("approval: Approve command execution?", mention="@neither_be_nor_me") == (
        "@neither_be_nor_me <b>approval</b>: Approve command execution?"
    )
    assert format_telegram_text("assistant/final_answer: ship it", mention="@neither_be_nor_me") == (
        "@neither_be_nor_me <b>assistant/final_answer</b>: ship it"
    )
    assert format_telegram_text("assistant/commentary: still working", mention="@neither_be_nor_me") == (
        "<b>assistant/commentary</b>: still working"
    )


def test_format_telegram_text_mentions_configured_username_for_attention_messages() -> None:
    assert format_telegram_text("approval: Approve command execution?", mention="@oleg") == (
        "@oleg <b>approval</b>: Approve command execution?"
    )
    assert format_telegram_text("assistant/final_answer: ship it", mention="@oleg") == (
        "@oleg <b>assistant/final_answer</b>: ship it"
    )
    assert format_telegram_text("assistant/commentary: still working", mention="@oleg") == (
        "<b>assistant/commentary</b>: still working"
    )


def _seed_handoff_threads(state_path: Path) -> list[dict[str, object]]:
    thread_specs = [
        {"thread_id": "thr_older", "name": "Older Thread", "cwd": "/tmp/apps/app-one", "updated_at": 110, "active": True},
        {"thread_id": "thr_recent", "name": "Recent Thread", "cwd": "/tmp/apps/app-two", "updated_at": 190, "active": True},
        {"thread_id": "thr_middle", "name": "Middle Thread", "cwd": "/tmp/apps/app-three", "updated_at": 150, "active": True},
        {"thread_id": "thr_latest", "name": "Latest Thread", "cwd": "/tmp/apps/app-four", "updated_at": 210, "active": True},
        {"thread_id": "thr_idle", "name": "Idle Thread", "cwd": "/tmp/apps/app-five", "updated_at": 205, "active": False},
        {"thread_id": "thr_keep", "name": "Keep Thread", "cwd": "/tmp/apps/app-six", "updated_at": 170, "active": True},
        {"thread_id": "thr_cut", "name": "Cut Thread", "cwd": "/tmp/apps/app-seven", "updated_at": 90, "active": True},
    ]
    turns: dict[str, object] = {}
    threads: dict[str, object] = {}
    for index, spec in enumerate(thread_specs, start=1):
        turn_id = f"turn_{index}"
        turns[turn_id] = {
            "id": turn_id,
            "threadId": spec["thread_id"],
            "status": "inProgress" if spec["active"] else "completed",
            "items": [
                {
                    "id": f"item_user_{index}",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": f"prompt {index}"}],
                }
            ],
            "error": None,
            "summary": None if spec["active"] else f"Completed: prompt {index}",
        }
        threads[str(spec["thread_id"])] = {
            "id": spec["thread_id"],
            "name": spec["name"],
            "preview": f"prompt {index}",
            "cwd": spec["cwd"],
            "createdAt": index,
            "updatedAt": spec["updated_at"],
            "archived": False,
            "status": {"type": "active"} if spec["active"] else {"type": "idle"},
            "activeTurnId": turn_id if spec["active"] else None,
            "turns": [turn_id],
        }
    state = {
        "threads": threads,
        "turns": turns,
        "loaded": [],
        "counter": 500,
        "pending_approvals": {},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return thread_specs


def test_cli_listen_and_send_uses_telegram_interface(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 50,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "listen-and-send",
            thread_id,
            "--interface",
            "telegram",
            "--max-events",
            "2",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    assert result.exit_code == 0
    assert "Listening and sending on thread" in result.stdout
    sent_texts = [str(message["text"]) for message in fake_api.sent_messages]
    assert any("Telegram interface is active" in text for text in sent_texts)
    assert sent_texts.count("<b>user</b>: telegram follow up") == 1
    assert any("Started turn" in text for text in sent_texts)
    assert fake_api.sent_messages[-1]["message_thread_id"] == 321


def test_cli_listen_and_send_prefers_configured_default_chat_over_stale_cached_session(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(TelegramSessionRecord(thread_id=thread_id, chat_id=123456789))
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 51,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "listen-and-send",
            thread_id,
            "--interface",
            "telegram",
            "--max-events",
            "2",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    assert result.exit_code == 0
    sent_chat_ids = [int(message["chat_id"]) for message in fake_api.sent_messages]
    assert sent_chat_ids
    assert set(sent_chat_ids) == {777}


def test_cli_listen_and_send_starts_fresh_telegram_session_each_run(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(
        TelegramSessionRecord(
            thread_id=thread_id,
            chat_id=123456789,
            message_thread_id=999,
            last_update_id=998,
            chat_username="old-user",
        )
    )
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 52,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ]
        ]
    )
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "listen-and-send",
            thread_id,
            "--interface",
            "telegram",
            "--max-events",
            "2",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    session = registry.get_telegram_session(thread_id)

    assert result.exit_code == 0
    assert session is not None
    assert session.chat_id == 777
    assert session.message_thread_id == 321
    assert session.last_update_id == 52
    assert session.chat_username == "oleg"


def test_cli_listen_and_send_telegram_approval_uses_buttons(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 60,
                    "message": {
                        "message_id": 1,
                        "text": "git commit",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ],
            [
                {
                    "update_id": 61,
                    "message": {
                        "message_id": 2,
                        "text": "approve",
                        "chat": {"id": 777, "type": "private"},
                        "message_thread_id": 321,
                        "from": {"username": "oleg"},
                    },
                }
            ],
        ]
    )
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "listen-and-send",
            thread_id,
            "--interface",
            "telegram",
            "--max-events",
            "6",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    assert result.exit_code == 0
    approval_message = next(message for message in fake_api.sent_messages if "<b>approval</b>:" in str(message["text"]))
    assert approval_message["reply_markup"] == {
        "keyboard": [[{"text": "approve"}, {"text": "cancel"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    approval_sent_message = next(
        message for message in fake_api.sent_messages if "<b>approval sent</b>:" in str(message["text"])
    )
    assert approval_sent_message["reply_markup"] == {"remove_keyboard": True}


@pytest.mark.asyncio
async def test_telegram_bridge_mentions_operator_for_approval_and_final_messages(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], username="@oleg", default_chat_id=777)
    api = FakeTelegramApi()
    bridge = TelegramOperatorBridge(thread_id="thr_attention", registry=registry, config=config, api=api)

    await bridge.start()
    session = registry.get_telegram_session("thr_attention")
    assert session is not None

    registry.save_telegram_session(session.model_copy(update={"chat_username": "someone-else"}))
    bridge._session = registry.get_telegram_session("thr_attention")  # type: ignore[attr-defined]

    await bridge.send_text("approval: Approve command execution?")
    await bridge.send_text("assistant/final_answer: completed")
    await bridge.send_text("assistant/commentary: progress update")
    await bridge.close()

    sent_texts = [str(message["text"]) for message in api.sent_messages]
    assert "@oleg <b>approval</b>: Approve command execution?" in sent_texts
    assert "@oleg <b>assistant/final_answer</b>: completed" in sent_texts
    assert "<b>assistant/commentary</b>: progress update" in sent_texts


def test_cli_hand_off_attaches_five_most_recent_active_threads_to_telegram_topics(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_specs = _seed_handoff_threads(state_path)
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(
        TelegramSessionRecord(thread_id="thr_previous", chat_id=777, message_thread_id=444, topic_name="old topic")
    )
    registry.save_telegram_session(
        TelegramSessionRecord(thread_id="thr_other_chat", chat_id=999, message_thread_id=555, topic_name="other chat topic")
    )
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi()
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "hand-off",
            "--max-events",
            "0",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    selected_specs = sorted(
        [spec for spec in thread_specs if spec["active"]],
        key=lambda spec: int(spec["updated_at"]),
        reverse=True,
    )[:5]
    expected_topic_names = [
        build_topic_name(
            cwd=str(spec["cwd"]),
            thread_name=str(spec["name"]),
            thread_id=str(spec["thread_id"]),
        )
        for spec in selected_specs
    ]

    assert result.exit_code == 0
    assert "Handing off 5 thread(s) to Telegram chat 777" in result.stdout
    assert fake_api.deleted_topics == [{"chat_id": 777, "message_thread_id": 444}]
    assert [str(topic["name"]) for topic in fake_api.created_topics] == expected_topic_names
    assert [int(topic["chat_id"]) for topic in fake_api.created_topics] == [777] * 5
    assert fake_api.closed is True

    for index, spec in enumerate(selected_specs, start=1):
        session = registry.get_telegram_session(str(spec["thread_id"]))
        assert session is not None
        assert session.chat_id == 777
        assert session.message_thread_id == 1000 + index
        assert session.topic_name == expected_topic_names[index - 1]

    assert registry.get_telegram_session("thr_idle") is None
    assert registry.get_telegram_session("thr_cut") is None
    assert registry.get_telegram_session("thr_previous") is None
    assert registry.get_telegram_session("thr_other_chat") is not None


def test_cli_hand_off_backfills_with_recent_idle_threads_when_active_threads_are_fewer_than_limit(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    state = {
        "threads": {
            "thr_active_1": {
                "id": "thr_active_1",
                "name": "Active One",
                "preview": "a1",
                "cwd": "/tmp/apps/project-a",
                "createdAt": 1,
                "updatedAt": 300,
                "archived": False,
                "status": {"type": "active"},
                "activeTurnId": "turn_active_1",
                "turns": ["turn_active_1"],
            },
            "thr_idle_1": {
                "id": "thr_idle_1",
                "name": "Idle One",
                "preview": "i1",
                "cwd": "/tmp/apps/project-b",
                "createdAt": 2,
                "updatedAt": 290,
                "archived": False,
                "status": {"type": "idle"},
                "activeTurnId": None,
                "turns": ["turn_idle_1"],
            },
            "thr_idle_2": {
                "id": "thr_idle_2",
                "name": "Idle Two",
                "preview": "i2",
                "cwd": "/tmp/apps/project-c",
                "createdAt": 3,
                "updatedAt": 280,
                "archived": False,
                "status": {"type": "idle"},
                "activeTurnId": None,
                "turns": ["turn_idle_2"],
            },
        },
        "turns": {
            "turn_active_1": {
                "id": "turn_active_1",
                "threadId": "thr_active_1",
                "status": "inProgress",
                "items": [{"id": "item_active_1", "type": "userMessage", "content": [{"type": "text", "text": "a1"}]}],
                "error": None,
                "summary": None,
            },
            "turn_idle_1": {
                "id": "turn_idle_1",
                "threadId": "thr_idle_1",
                "status": "completed",
                "items": [{"id": "item_idle_1", "type": "userMessage", "content": [{"type": "text", "text": "i1"}]}],
                "error": None,
                "summary": "done",
            },
            "turn_idle_2": {
                "id": "turn_idle_2",
                "threadId": "thr_idle_2",
                "status": "completed",
                "items": [{"id": "item_idle_2", "type": "userMessage", "content": [{"type": "text", "text": "i2"}]}],
                "error": None,
                "summary": "done",
            },
        },
        "loaded": [],
        "counter": 900,
        "pending_approvals": {},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env = _telegram_env(state_path)
    fake_api = FakeTelegramApi()
    monkeypatch.setattr("cli.HttpTelegramBotApi", lambda config: fake_api)

    result = runner.invoke(
        app,
        [
            "hand-off",
            "--limit",
            "3",
            "--max-events",
            "0",
            "--config",
            str(config_path),
        ],
        env=env,
    )

    assert result.exit_code == 0
    assert "Handing off 3 thread(s) to Telegram chat 777" in result.stdout
    assert [str(topic["name"]) for topic in fake_api.created_topics] == [
        "project-a | Active One",
        "project-b | Idle One",
        "project-c | Idle Two",
    ]
