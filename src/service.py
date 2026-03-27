from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from adapter import AppServerAdapter
from config import AppConfig
from ingestion import EventIngestor
from models import ConnectionState, EventRecord, ThreadRecord, TurnRecord, utc_now_iso
from registry import JsonRegistry
from transport import StdioJsonRpcTransport


@dataclass(slots=True)
class StartResult:
    thread: ThreadRecord
    turn: TurnRecord


class OrchestratorService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.registry = JsonRegistry(config.data_dir)
        self.transport = StdioJsonRpcTransport(
            config.app_server_command,
            cwd=str(config.app_server_cwd) if config.app_server_cwd is not None else None,
        )
        self.adapter = AppServerAdapter(self.transport, config)
        self.ingestor = EventIngestor(self.registry)
        self.adapter.add_notification_handler(self._handle_notification)
        self._connected = False

    async def connect(self) -> ConnectionState:
        if self._connected:
            state = self.registry.get_connection_state()
            return state or ConnectionState(app_server_instance=self.config.app_server_instance)
        await self.transport.connect()
        initialize = await self.adapter.initialize_client()
        state = ConnectionState(
            app_server_instance=self.config.app_server_instance,
            initialized_at=utc_now_iso(),
            last_error=None,
            platform_family=initialize.platform_family,
            platform_os=initialize.platform_os,
            user_agent=initialize.user_agent,
        )
        self.registry.save_connection_state(state)
        self._connected = True
        return state

    async def close(self) -> None:
        await self.transport.close()

    async def list_threads(self, limit: int = 50) -> list[ThreadRecord]:
        response = await self.adapter.list_threads({"limit": limit, "cursor": None, "sortKey": "created_at"})
        for thread in response.data:
            self.ingestor.project_thread_snapshot(thread.model_dump(by_alias=True))
        return self.registry.list_threads()

    async def read_thread(self, thread_id: str, include_turns: bool = True) -> ThreadRecord:
        response = await self.adapter.read_thread(thread_id, include_turns=include_turns)
        return self.ingestor.project_thread_snapshot(response.thread.model_dump(by_alias=True))

    async def resume_thread(self, thread_id: str) -> ThreadRecord:
        response = await self.adapter.resume_thread(thread_id)
        return self.ingestor.project_thread_snapshot(response.thread.model_dump(by_alias=True))

    async def start_new_thread(self, prompt: str) -> StartResult:
        thread_envelope = await self.adapter.start_thread({})
        thread = self.ingestor.project_thread_snapshot(thread_envelope.thread.model_dump(by_alias=True))
        turn_options = self.config.turn_start_options or None
        turn_envelope = await self.adapter.start_turn(thread.thread_id, prompt, options=turn_options)
        turn = self.ingestor.project_turn_snapshot(turn_envelope.turn.model_dump(by_alias=True), thread_id=thread.thread_id)
        thread = self.registry.get_thread(thread.thread_id) or thread
        return StartResult(thread=thread, turn=turn)

    async def start_turn_on_thread(self, thread_id: str, prompt: str) -> TurnRecord:
        response = await self.adapter.resume_thread(thread_id)
        self.ingestor.project_thread_snapshot(response.thread.model_dump(by_alias=True))
        turn_options = self.config.turn_start_options or None
        turn_envelope = await self.adapter.start_turn(thread_id, prompt, options=turn_options)
        turn = self.ingestor.project_turn_snapshot(turn_envelope.turn.model_dump(by_alias=True), thread_id=thread_id)
        return turn

    def inspect_local(self, thread_id: str) -> tuple[ThreadRecord | None, list[TurnRecord]]:
        thread = self.registry.get_thread(thread_id)
        turns = self.registry.list_turns(thread_id=thread_id)
        return thread, turns

    async def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "data_dir": {"ok": True, "path": str(self.config.data_dir)},
            "server_command": {"ok": bool(self.config.app_server_command), "value": self.config.app_server_instance},
            "telegram": {
                "ok": True if not self.config.telegram.enabled else bool(self.config.telegram.bot_token),
                "enabled": self.config.telegram.enabled,
                "default_chat_id": self.config.telegram.default_chat_id,
                "allowed_chat_ids": len(self.config.telegram.allowed_chat_ids),
            },
        }
        if self.config.app_server_command:
            try:
                state = await self.connect()
                checks["connect"] = {"ok": True, "user_agent": state.user_agent, "platform_os": state.platform_os}
            except Exception as exc:  # noqa: BLE001
                checks["connect"] = {"ok": False, "error": str(exc)}
        return checks

    async def listen(
        self,
        thread_id: str,
        on_event: Callable[[EventRecord], Any],
        *,
        max_events: int | None = None,
    ) -> int:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue()

        def listener(event: EventRecord) -> None:
            if event.thread_id == thread_id:
                queue.put_nowait(event)

        seen = 0
        unsubscribe = self.ingestor.subscribe(listener)
        try:
            await self.resume_thread(thread_id)
            while max_events is None or seen < max_events:
                event = await queue.get()
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
                seen += 1
        finally:
            unsubscribe()
        return seen

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        self.ingestor.handle_notification(method, params)
