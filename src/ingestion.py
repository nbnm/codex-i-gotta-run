from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from models import EventRecord, PlanEntry, ThreadRecord, TurnRecord, utc_now_iso
from registry import JsonRegistry


EventSubscriber = Callable[[EventRecord], None]


class EventIngestor:
    def __init__(self, registry: JsonRegistry) -> None:
        self._registry = registry
        self._subscribers: list[EventSubscriber] = []

    def subscribe(self, callback: EventSubscriber) -> Callable[[], None]:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def project_thread_snapshot(self, payload: dict[str, Any]) -> ThreadRecord:
        thread_id = payload["id"]
        existing = self._registry.get_thread(thread_id) or ThreadRecord(thread_id=thread_id)
        status_payload = payload.get("status") or existing.status_payload
        active_turn_id = payload.get("activeTurnId", existing.active_turn_id)
        record = ThreadRecord(
            thread_id=thread_id,
            name=payload.get("name", existing.name),
            preview=payload.get("preview", existing.preview),
            cwd=payload.get("cwd", existing.cwd),
            source_kind=payload.get("sourceKind", existing.source_kind),
            model_provider=payload.get("modelProvider", existing.model_provider),
            created_at=payload.get("createdAt", existing.created_at),
            updated_at=payload.get("updatedAt", existing.updated_at),
            status_type=(status_payload or {}).get("type", existing.status_type),
            status_payload=status_payload or {},
            active_turn_id=active_turn_id,
            last_seen_at=utc_now_iso(),
            archived=payload.get("archived", existing.archived),
            raw_thread=payload,
        )
        self._registry.save_thread(record)
        for turn in payload.get("turns", []):
            self.project_turn_snapshot(turn, thread_id=thread_id)
        return record

    def project_turn_snapshot(self, payload: dict[str, Any], *, thread_id: str | None = None) -> TurnRecord:
        turn_id = payload["id"]
        existing = self._registry.get_turn(turn_id) or TurnRecord(turn_id=turn_id)
        resolved_thread_id = payload.get("threadId") or thread_id or existing.thread_id
        status = payload.get("status", existing.status)
        started_at = existing.started_at
        completed_at = existing.completed_at
        if status == "inProgress" and started_at is None:
            started_at = utc_now_iso()
        if status in {"completed", "interrupted", "failed"} and completed_at is None:
            completed_at = utc_now_iso()
        plan_payload = payload.get("plan")
        plan = existing.plan
        if isinstance(plan_payload, list):
            plan = [PlanEntry.model_validate(entry) for entry in plan_payload]
        turn = TurnRecord(
            turn_id=turn_id,
            thread_id=resolved_thread_id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            summary=payload.get("summary", existing.summary),
            error_json=payload.get("error", existing.error_json),
            diff=payload.get("diff", existing.diff),
            plan=plan,
            raw_turn=payload,
        )
        self._registry.save_turn(turn)
        if resolved_thread_id is not None:
            thread = self._registry.get_thread(resolved_thread_id) or ThreadRecord(thread_id=resolved_thread_id)
            active_turn_id = thread.active_turn_id
            status_type = thread.status_type
            if status == "inProgress":
                active_turn_id = turn_id
                status_type = "active"
            elif active_turn_id == turn_id and status in {"completed", "interrupted", "failed"}:
                active_turn_id = None
                status_type = "idle"
            updated_thread = thread.model_copy(
                update={
                    "active_turn_id": active_turn_id,
                    "status_type": status_type,
                    "last_seen_at": utc_now_iso(),
                }
            )
            self._registry.save_thread(updated_thread)
        return turn

    def handle_notification(self, method: str, params: dict[str, Any]) -> EventRecord:
        thread_id, turn_id = self._extract_ids(method, params)
        event = EventRecord(
            id=str(uuid.uuid4()),
            thread_id=thread_id,
            turn_id=turn_id,
            event_type=method,
            payload_json=params,
        )
        self._registry.append_event(event)
        self._apply_projection(event)
        self._update_connection_state(event.received_at)
        for callback in list(self._subscribers):
            callback(event)
        return event

    def _update_connection_state(self, timestamp: str) -> None:
        state = self._registry.get_connection_state()
        if state is None:
            return
        self._registry.save_connection_state(state.model_copy(update={"last_event_at": timestamp}))

    def _apply_projection(self, event: EventRecord) -> None:
        params = event.payload_json
        method = event.event_type
        if method == "thread/started" and "thread" in params:
            self.project_thread_snapshot(params["thread"])
            return
        if method == "thread/status/changed" and event.thread_id:
            thread = self._registry.get_thread(event.thread_id) or ThreadRecord(thread_id=event.thread_id)
            status_payload = params.get("status") or {}
            self._registry.save_thread(
                thread.model_copy(
                    update={
                        "status_type": status_payload.get("type", thread.status_type),
                        "status_payload": status_payload,
                        "last_seen_at": event.received_at,
                    }
                )
            )
            return
        if method == "thread/archived" and event.thread_id:
            thread = self._registry.get_thread(event.thread_id) or ThreadRecord(thread_id=event.thread_id)
            self._registry.save_thread(
                thread.model_copy(update={"archived": True, "last_seen_at": event.received_at})
            )
            return
        if method == "thread/unarchived" and event.thread_id:
            thread = self._registry.get_thread(event.thread_id) or ThreadRecord(thread_id=event.thread_id)
            self._registry.save_thread(
                thread.model_copy(update={"archived": False, "last_seen_at": event.received_at})
            )
            return
        if method == "thread/closed" and event.thread_id:
            thread = self._registry.get_thread(event.thread_id) or ThreadRecord(thread_id=event.thread_id)
            self._registry.save_thread(
                thread.model_copy(
                    update={
                        "status_type": "notLoaded",
                        "active_turn_id": None,
                        "last_seen_at": event.received_at,
                    }
                )
            )
            return
        if method == "turn/started" and "turn" in params:
            self.project_turn_snapshot(params["turn"], thread_id=event.thread_id)
            return
        if method == "turn/completed" and "turn" in params:
            self.project_turn_snapshot(params["turn"], thread_id=event.thread_id)
            return
        if method == "turn/diff/updated" and event.turn_id:
            turn = self._registry.get_turn(event.turn_id) or TurnRecord(turn_id=event.turn_id, thread_id=event.thread_id)
            self._registry.save_turn(turn.model_copy(update={"diff": params.get("diff"), "raw_turn": turn.raw_turn}))
            return
        if method == "turn/plan/updated" and event.turn_id:
            turn = self._registry.get_turn(event.turn_id) or TurnRecord(turn_id=event.turn_id, thread_id=event.thread_id)
            plan_payload = params.get("plan", [])
            plan = [PlanEntry.model_validate(entry) for entry in plan_payload]
            self._registry.save_turn(turn.model_copy(update={"plan": plan}))
            return
        if event.thread_id:
            thread = self._registry.get_thread(event.thread_id)
            if thread is not None:
                self._registry.save_thread(thread.model_copy(update={"last_seen_at": event.received_at}))

    def _extract_ids(self, method: str, params: dict[str, Any]) -> tuple[str | None, str | None]:
        thread_id = params.get("threadId") or params.get("conversationId")
        turn_id = params.get("turnId")
        thread_payload = params.get("thread")
        turn_payload = params.get("turn")
        item_payload = params.get("item")
        msg_payload = params.get("msg")

        if thread_id is None and isinstance(thread_payload, dict):
            thread_id = thread_payload.get("id")
        if turn_id is None and isinstance(turn_payload, dict):
            turn_id = turn_payload.get("id")
        if thread_id is None and isinstance(turn_payload, dict):
            thread_id = turn_payload.get("threadId")
        if turn_id is None and isinstance(item_payload, dict):
            turn_id = item_payload.get("turnId")
        if thread_id is None and isinstance(item_payload, dict):
            thread_id = item_payload.get("threadId")
        if thread_id is None and isinstance(msg_payload, dict):
            thread_id = msg_payload.get("threadId") or msg_payload.get("conversationId")
        if thread_id is None and turn_id is not None:
            known_turn = self._registry.get_turn(turn_id)
            if known_turn is not None:
                thread_id = known_turn.thread_id
        if turn_id is None and thread_id is not None and method.startswith("thread/"):
            known_thread = self._registry.get_thread(thread_id)
            if known_thread is not None:
                turn_id = known_thread.active_turn_id
        return thread_id, turn_id
