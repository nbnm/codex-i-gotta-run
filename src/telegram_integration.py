from __future__ import annotations

import asyncio
import contextlib
import html
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from models import TelegramConfig, TelegramSessionRecord, utc_now_iso
from registry import JsonRegistry

TELEGRAM_TEXT_LIMIT = 4096


class TelegramApi(Protocol):
    async def create_forum_topic(self, chat_id: int, name: str) -> dict[str, Any]: ...

    async def delete_forum_topic(self, chat_id: int, message_thread_id: int) -> None: ...

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]: ...

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


class HttpTelegramBotApi:
    def __init__(self, config: TelegramConfig) -> None:
        if not config.bot_token:
            raise ValueError("Telegram bot token is not configured.")
        base_url = config.api_base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=f"{base_url}/bot{config.bot_token}", timeout=config.poll_timeout_seconds + 10)

    async def get_updates(self, *, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_seconds}
        if offset is not None:
            payload["offset"] = offset
        response = await self._client.post("/getUpdates", json=payload)
        response.raise_for_status()
        result = response.json()
        return list(result.get("result", []))

    async def create_forum_topic(self, chat_id: int, name: str) -> dict[str, Any]:
        response = await self._client.post("/createForumTopic", json={"chat_id": chat_id, "name": name})
        response.raise_for_status()
        result = response.json()
        return dict(result.get("result", {}))

    async def delete_forum_topic(self, chat_id: int, message_thread_id: int) -> None:
        response = await self._client.post(
            "/deleteForumTopic",
            json={"chat_id": chat_id, "message_thread_id": message_thread_id},
        )
        response.raise_for_status()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if message_thread_id is not None:
            payload["message_thread_id"] = message_thread_id
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await self._client.post("/sendMessage", json=payload)
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


class TelegramOperatorBridge:
    def __init__(
        self,
        *,
        thread_id: str,
        registry: JsonRegistry,
        config: TelegramConfig,
        api: TelegramApi,
        chat_id: int | None = None,
        topic_name: str | None = None,
        poll_updates: bool = True,
        owns_api: bool = True,
    ) -> None:
        self._thread_id = thread_id
        self._registry = registry
        self._config = config
        self._api = api
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._topic_name = topic_name
        self._poll_updates = poll_updates
        self._owns_api = owns_api
        existing = registry.get_telegram_session(thread_id) or TelegramSessionRecord(thread_id=thread_id)
        bound_chat_id = (
            chat_id
            if chat_id is not None
            else config.default_chat_id
            if config.default_chat_id is not None
            else existing.chat_id
        )
        self._session = existing.model_copy(update={"chat_id": bound_chat_id})
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False
        self._pending_messages: list[str] = []

    @property
    def bound_chat_id(self) -> int | None:
        return self._session.chat_id

    async def start(self) -> None:
        if self._topic_name and self._session.chat_id is None:
            raise ValueError("Telegram topic creation requires default_chat_id or --telegram-chat-id.")
        if self._topic_name and self._session.message_thread_id is None:
            topic = await self._api.create_forum_topic(self._session.chat_id, self._topic_name)
            self._save_session(
                message_thread_id=topic.get("message_thread_id"),
                topic_name=str(topic.get("name") or self._topic_name),
            )
        self._registry.save_telegram_session(self._session)
        await self._flush_pending_messages()
        if self._poll_updates:
            self._poll_task = asyncio.create_task(self._poll_loop())
        await self.send_text(
            f"Attached to thread {self._thread_id}. Send a message to start the next turn. "
            "Use approve or cancel when a command approval is requested."
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self._owns_api:
            await self._api.close()

    async def read_input(self) -> str:
        return await self._input_queue.get()

    async def send_text(
        self,
        text: str,
        *,
        buttons: list[str] | None = None,
        clear_buttons: bool = False,
    ) -> None:
        chunks = _chunk_text(
            format_telegram_text(text, mention=_telegram_attention_mention(self._session, self._config.username)),
            TELEGRAM_TEXT_LIMIT,
        )
        if self._session.chat_id is None:
            self._pending_messages.extend(chunks)
            return
        reply_markup = _reply_keyboard(buttons) if buttons else _remove_keyboard() if clear_buttons else None
        for index, chunk in enumerate(chunks):
            await self._api.send_message(
                self._session.chat_id,
                chunk,
                parse_mode="HTML",
                message_thread_id=self._session.message_thread_id,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        self._save_session(last_outbound_at=utc_now_iso())

    async def _poll_loop(self) -> None:
        while True:
            offset = self._next_offset()
            updates = await self._api.get_updates(offset=offset, timeout_seconds=self._config.poll_timeout_seconds)
            for update in updates:
                await self.handle_update(update)

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self._save_session(last_update_id=update_id)

        message = update.get("message")
        if not isinstance(message, dict):
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = chat.get("id")
        message_thread_id = message.get("message_thread_id")
        if not isinstance(chat_id, int):
            return
        username = str(from_user.get("username") or "").lstrip("@").lower()
        if not self._is_allowed(chat_id=chat_id, username=username):
            return
        if self._session.chat_id is None:
            self._save_session(
                chat_id=chat_id,
                chat_username=username or None,
                chat_type=chat.get("type"),
                last_inbound_at=utc_now_iso(),
            )
            await self._flush_pending_messages()
            await self.send_text(f"Attached to thread {self._thread_id}. Send text to start the next turn.")
        elif chat_id != self._session.chat_id:
            return
        elif self._session.message_thread_id is not None and message_thread_id != self._session.message_thread_id:
            return
        else:
            self._save_session(
                message_thread_id=message_thread_id or self._session.message_thread_id,
                chat_username=username or self._session.chat_username,
                chat_type=chat.get("type") or self._session.chat_type,
                last_inbound_at=utc_now_iso(),
            )

        normalized = text.strip()
        if normalized == "/start":
            await self.send_text(f"Thread {self._thread_id} is active. Send text to start the next turn.")
            return
        if normalized == "/help":
            await self.send_text("Send text to start the next turn. Use the approval buttons when prompted.")
            return
        if normalized in {"/attach", "/bind"}:
            await self.send_text(f"Thread {self._thread_id} is already attached to this chat.")
            return
        await self._input_queue.put(normalized)

    def _is_allowed(self, *, chat_id: int, username: str) -> bool:
        allowed_chat_ids = set(self._config.allowed_chat_ids)
        allowed_username = (self._config.username or "").lstrip("@").lower()
        if allowed_chat_ids and chat_id not in allowed_chat_ids:
            return False
        if allowed_username and username != allowed_username:
            return False
        return True

    def _next_offset(self) -> int | None:
        if self._session.last_update_id is None:
            return None
        return self._session.last_update_id + 1

    async def _flush_pending_messages(self) -> None:
        if self._session.chat_id is None or not self._pending_messages:
            return
        pending = list(self._pending_messages)
        self._pending_messages.clear()
        for chunk in pending:
            await self._api.send_message(
                self._session.chat_id,
                chunk,
                parse_mode="HTML",
                message_thread_id=self._session.message_thread_id,
            )
        self._save_session(last_outbound_at=utc_now_iso())

    def _save_session(self, **updates: Any) -> None:
        self._session = self._session.model_copy(update=updates)
        self._registry.save_telegram_session(self._session)


def _chunk_text(text: str, limit: int) -> Sequence[str]:
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def build_topic_name(*, cwd: str | None, thread_name: str | None, thread_id: str) -> str:
    project_name = (cwd or "").rstrip("/").split("/")[-1] or "unknown-project"
    label = thread_name or thread_id[:8]
    return f"{project_name} | {label}"[:128]


class TelegramBridgeHub:
    def __init__(self, *, api: TelegramApi, poll_timeout_seconds: int) -> None:
        self._api = api
        self._poll_timeout_seconds = poll_timeout_seconds
        self._bridges: list[TelegramOperatorBridge] = []
        self._poll_task: asyncio.Task[None] | None = None
        self._closed = False
        self._last_update_id: int | None = None

    def add_bridge(self, bridge: TelegramOperatorBridge) -> None:
        self._bridges.append(bridge)

    async def start(self) -> None:
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        await self._api.close()

    async def _poll_loop(self) -> None:
        while True:
            offset = None if self._last_update_id is None else self._last_update_id + 1
            updates = await self._api.get_updates(offset=offset, timeout_seconds=self._poll_timeout_seconds)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._last_update_id = update_id
                for bridge in list(self._bridges):
                    await bridge.handle_update(update)

def _reply_keyboard(buttons: list[str]) -> dict[str, Any]:
    return {
        "keyboard": [[{"text": button} for button in buttons]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
    }


def _remove_keyboard() -> dict[str, Any]:
    return {"remove_keyboard": True}


def _telegram_attention_mention(session: TelegramSessionRecord, configured_username: str | None) -> str | None:
    if configured_username:
        return html.escape(f"@{configured_username.lstrip('@')}")
    if session.chat_id is not None:
        return f'<a href="tg://user?id={session.chat_id}">@{session.chat_id}</a>'
    return None


def _needs_attention_mention(prefix: str) -> bool:
    normalized = prefix.strip().lower()
    return normalized == "approval" or normalized.startswith("assistant/final")


def format_telegram_text(text: str, *, mention: str | None = None) -> str:
    prefix, separator, rest = text.partition(":")
    if not separator:
        return html.escape(text)
    formatted = f"<b>{html.escape(prefix)}</b>:{html.escape(rest)}"
    if mention and _needs_attention_mention(prefix):
        return f"{mention} {formatted}"
    return formatted
