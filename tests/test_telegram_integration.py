from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from models import TelegramConfig, TelegramSessionRecord
from registry import JsonRegistry
from telegram_integration import TelegramOperatorBridge, format_telegram_text


def _write_telegram_config(tmp_path: Path, *, default_chat_id: int | None = None) -> tuple[Path, Path]:
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
                'bot_token = "test-token"',
                "allowed_chat_ids = [777]",
                'allowed_usernames = ["oleg"]',
                *( [f"default_chat_id = {default_chat_id}", ""] if default_chat_id is not None else [] ),
                "",
            ]
        ),
        encoding="utf-8",
    )
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
        self.closed = False

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
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_telegram_bridge_binds_authorized_chat_and_flushes_buffer(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], allowed_usernames=["oleg"])
    api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 10,
                    "message": {
                        "message_id": 1,
                        "text": "new follow up",
                        "chat": {"id": 777, "type": "private"},
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
    session = registry.get_telegram_session("thr_1")
    await bridge.close()

    assert inbound == "new follow up"
    assert session is not None
    assert session.chat_id == 777
    assert session.last_update_id == 10
    assert api.sent_messages[0]["chat_id"] == 777
    assert api.sent_messages[0]["text"] == "buffered before attach"
    assert api.sent_messages[0]["parse_mode"] == "HTML"
    assert api.closed is True


@pytest.mark.asyncio
async def test_telegram_bridge_ignores_unauthorized_messages(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    config = TelegramConfig(bot_token="test-token", allowed_chat_ids=[777], allowed_usernames=["oleg"])
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

    assert registry.get_telegram_session("thr_2") is not None
    assert registry.get_telegram_session("thr_2").chat_id is None
    assert api.sent_messages == []


@pytest.mark.asyncio
async def test_telegram_bridge_prefers_configured_default_chat_over_stale_cached_session(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(TelegramSessionRecord(thread_id="thr_3", chat_id=123456789))
    config = TelegramConfig(
        bot_token="test-token",
        allowed_chat_ids=[777],
        allowed_usernames=["oleg"],
        default_chat_id=777,
    )
    api = FakeTelegramApi()

    bridge = TelegramOperatorBridge(thread_id="thr_3", registry=registry, config=config, api=api)
    await bridge.start()
    await bridge.send_text("hello")
    session = registry.get_telegram_session("thr_3")
    await bridge.close()

    assert session is not None
    assert session.chat_id == 777
    assert api.sent_messages[0]["chat_id"] == 777
    assert api.sent_messages[0]["text"] == (
        "Attached to thread thr_3. Send a message to start the next turn. Use approve or cancel when a command approval is requested."
    )
    assert api.sent_messages[1]["chat_id"] == 777
    assert api.sent_messages[1]["text"] == "hello"


def test_format_telegram_text_bolds_prefix() -> None:
    assert format_telegram_text("assistant/commentary: hello <world>") == "<b>assistant/commentary</b>: hello &lt;world&gt;"


def test_cli_listen_and_send_uses_telegram_interface(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    env = {"FAKE_APP_SERVER_STATE_PATH": str(state_path)}
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 50,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
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


def test_cli_listen_and_send_prefers_configured_default_chat_over_stale_cached_session(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    registry = JsonRegistry(tmp_path / "registry")
    registry.save_telegram_session(TelegramSessionRecord(thread_id=thread_id, chat_id=123456789))
    env = {"FAKE_APP_SERVER_STATE_PATH": str(state_path)}
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 51,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
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
            last_update_id=999,
            chat_username="old-user",
        )
    )
    env = {"FAKE_APP_SERVER_STATE_PATH": str(state_path)}
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 52,
                    "message": {
                        "message_id": 1,
                        "text": "telegram follow up",
                        "chat": {"id": 777, "type": "private"},
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
    assert session.last_update_id == 52
    assert session.chat_username == "oleg"


def test_cli_listen_and_send_telegram_approval_uses_buttons(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, state_path = _write_telegram_config(tmp_path, default_chat_id=777)
    thread_id = _seed_thread(state_path, prompt="completed prompt")
    env = {"FAKE_APP_SERVER_STATE_PATH": str(state_path)}
    fake_api = FakeTelegramApi(
        updates=[
            [
                {
                    "update_id": 60,
                    "message": {
                        "message_id": 1,
                        "text": "git commit",
                        "chat": {"id": 777, "type": "private"},
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
    approval_message = next(message for message in fake_api.sent_messages if str(message["text"]).startswith("<b>approval</b>:"))
    assert approval_message["reply_markup"] == {
        "keyboard": [[{"text": "approve"}, {"text": "cancel"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }
    approval_sent_message = next(
        message for message in fake_api.sent_messages if str(message["text"]).startswith("<b>approval sent</b>:")
    )
    assert approval_sent_message["reply_markup"] == {"remove_keyboard": True}
