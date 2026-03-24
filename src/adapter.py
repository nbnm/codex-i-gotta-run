from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from config import AppConfig
from models import (
    InitializeResponse,
    ListThreadsResponse,
    SteerResult,
    ThreadEnvelope,
    TurnEnvelope,
    UnsubscribeResult,
)
from transport import StdioJsonRpcTransport

NotificationCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class AppServerAdapter:
    def __init__(self, transport: StdioJsonRpcTransport, config: AppConfig) -> None:
        self._transport = transport
        self._config = config
        self._initialized = False
        self._notification_callbacks: list[NotificationCallback] = []
        self._transport.add_notification_handler(self._dispatch_notification)

    def add_notification_handler(self, callback: NotificationCallback) -> None:
        self._notification_callbacks.append(callback)

    async def initialize_client(self) -> InitializeResponse:
        capabilities: dict[str, Any] = {}
        if self._config.experimental_api:
            capabilities["experimentalApi"] = True
        if self._config.opt_out_notification_methods:
            capabilities["optOutNotificationMethods"] = self._config.opt_out_notification_methods
        params: dict[str, Any] = {
            "clientInfo": self._config.client_info.model_dump(by_alias=True),
        }
        if capabilities:
            params["capabilities"] = capabilities
        result = await self._transport.request("initialize", params)
        await self._transport.notify("initialized", {})
        self._initialized = True
        return InitializeResponse.model_validate(result)

    async def list_threads(self, filters: dict[str, Any] | None = None) -> ListThreadsResponse:
        result = await self._transport.request("thread/list", filters or {})
        return ListThreadsResponse.model_validate(result)

    async def read_thread(self, thread_id: str, include_turns: bool = False) -> ThreadEnvelope:
        result = await self._transport.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        return ThreadEnvelope.model_validate(result)

    async def resume_thread(self, thread_id: str, options: dict[str, Any] | None = None) -> ThreadEnvelope:
        payload = {"threadId": thread_id}
        if options:
            payload.update(options)
        result = await self._transport.request("thread/resume", payload)
        return ThreadEnvelope.model_validate(result)

    async def start_thread(self, options: dict[str, Any] | None = None) -> ThreadEnvelope:
        result = await self._transport.request("thread/start", options or {})
        return ThreadEnvelope.model_validate(result)

    async def start_turn(self, thread_id: str, input_text: str, options: dict[str, Any] | None = None) -> TurnEnvelope:
        payload: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_text}],
        }
        if options:
            payload.update(options)
        result = await self._transport.request("turn/start", payload)
        return TurnEnvelope.model_validate(result)

    async def steer_turn(self, thread_id: str, turn_id: str, input_text: str) -> SteerResult:
        result = await self._transport.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": input_text}],
            },
        )
        return SteerResult.model_validate(result)

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self._transport.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def unsubscribe_thread(self, thread_id: str) -> UnsubscribeResult:
        result = await self._transport.request("thread/unsubscribe", {"threadId": thread_id})
        return UnsubscribeResult.model_validate(result)

    async def _dispatch_notification(self, method: str, params: dict[str, Any]) -> None:
        for callback in list(self._notification_callbacks):
            result = callback(method, params)
            if hasattr(result, "__await__"):
                await result
