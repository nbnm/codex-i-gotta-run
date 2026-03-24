from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from adapter import AppServerAdapter
from config import AppConfig
from ingestion import EventIngestor
from models import ConnectionState, EventRecord, QueuedInputRecord, ThreadRecord, TurnRecord, utc_now_iso
from registry import JsonRegistry
from steering import SteeringDecision, SteeringEngine
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
        self.steering = SteeringEngine(self.adapter, self.registry, self.ingestor)
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
        turn_envelope = await self.adapter.start_turn(thread.thread_id, prompt)
        turn = self.ingestor.project_turn_snapshot(turn_envelope.turn.model_dump(by_alias=True), thread_id=thread.thread_id)
        thread = self.registry.get_thread(thread.thread_id) or thread
        return StartResult(thread=thread, turn=turn)

    async def continue_thread(self, thread_id: str, prompt: str) -> SteeringDecision:
        return await self.steering.apply(thread_id, prompt, mode="continue")

    async def steer_thread(self, thread_id: str, turn_id: str, prompt: str) -> str:
        result = await self.adapter.steer_turn(thread_id, turn_id, prompt)
        return result.turn_id

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.adapter.interrupt_turn(thread_id, turn_id)

    def queue_input(self, thread_id: str, text: str, mode: str = "auto") -> QueuedInputRecord:
        item = QueuedInputRecord(id=f"queue_{uuid.uuid4().hex}", thread_id=thread_id, text=text, mode=mode)
        self.registry.save_queued_input(item)
        return item

    async def process_queue(self, thread_id: str) -> list[QueuedInputRecord]:
        return await self.steering.process_queue(thread_id)

    def inspect_local(self, thread_id: str) -> tuple[ThreadRecord | None, list[TurnRecord], list[QueuedInputRecord]]:
        thread = self.registry.get_thread(thread_id)
        turns = self.registry.list_turns(thread_id=thread_id)
        queue_items = self.registry.list_queued_inputs(thread_id=thread_id)
        return thread, turns, queue_items

    def status_snapshot(self) -> dict[str, Any]:
        threads = self.registry.list_threads()
        active = [thread for thread in threads if thread.active_turn_id]
        queued = self.registry.list_queued_inputs(statuses=["queued", "submitted"])
        return {
            "connection": self.registry.get_connection_state(),
            "active_threads": active,
            "queued_inputs": queued,
            "known_threads": threads,
        }

    async def doctor(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "data_dir": {"ok": True, "path": str(self.config.data_dir)},
            "server_command": {"ok": bool(self.config.app_server_command), "value": self.config.app_server_instance},
        }
        if self.config.app_server_command:
            try:
                state = await self.connect()
                checks["connect"] = {"ok": True, "user_agent": state.user_agent, "platform_os": state.platform_os}
            except Exception as exc:  # noqa: BLE001
                checks["connect"] = {"ok": False, "error": str(exc)}
        return checks

    async def tail_events(self, thread_id: str, *, max_events: int | None = None) -> list[EventRecord]:
        history = self.registry.list_events(thread_id=thread_id, limit=self.config.recent_event_limit)
        events: list[EventRecord] = list(history)
        queue: asyncio.Queue[EventRecord] = asyncio.Queue()

        def on_event(event: EventRecord) -> None:
            if event.thread_id == thread_id:
                queue.put_nowait(event)

        unsubscribe = self.ingestor.subscribe(on_event)
        try:
            await self.resume_thread(thread_id)
            while max_events is None or len(events) < max_events:
                event = await queue.get()
                events.append(event)
                if max_events is not None and len(events) >= max_events:
                    break
        finally:
            unsubscribe()
        return events

    def recent_events(self, thread_id: str, *, limit: int | None = None) -> list[EventRecord]:
        return self.registry.list_events(thread_id=thread_id, limit=limit or self.config.recent_event_limit)

    async def listen(
        self,
        thread_id: str,
        on_event: Callable[[EventRecord], None],
        *,
        max_events: int | None = None,
        include_history: bool = True,
    ) -> int:
        queue: asyncio.Queue[EventRecord] = asyncio.Queue()

        def listener(event: EventRecord) -> None:
            if event.thread_id == thread_id:
                queue.put_nowait(event)

        seen = 0
        seen_event_ids: set[str] = set()
        if include_history:
            for event in self.recent_events(thread_id):
                on_event(event)
                seen_event_ids.add(event.id)
                seen += 1
                if max_events is not None and seen >= max_events:
                    return seen
        unsubscribe = self.ingestor.subscribe(listener)
        try:
            await self.resume_thread(thread_id)
            while max_events is None or seen < max_events:
                event = await queue.get()
                if event.id in seen_event_ids:
                    continue
                on_event(event)
                seen_event_ids.add(event.id)
                seen += 1
        finally:
            unsubscribe()
        return seen

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        self.ingestor.handle_notification(method, params)
