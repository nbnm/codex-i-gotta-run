from __future__ import annotations

from dataclasses import dataclass

from adapter import AppServerAdapter
from ingestion import EventIngestor
from models import QueuedInputRecord, ThreadRecord, utc_now_iso
from registry import JsonRegistry


class SteeringError(RuntimeError):
    """Raised when steering cannot be completed deterministically."""


@dataclass(slots=True)
class SteeringDecision:
    action: str
    thread_id: str
    turn_id: str | None


class SteeringEngine:
    def __init__(self, adapter: AppServerAdapter, registry: JsonRegistry, ingestor: EventIngestor) -> None:
        self._adapter = adapter
        self._registry = registry
        self._ingestor = ingestor

    async def decide(self, thread_id: str, mode: str = "auto") -> SteeringDecision:
        thread = await self._refresh_thread(thread_id)
        if mode == "steer":
            if not thread.active_turn_id:
                raise SteeringError("No active turn is available for steering.")
            return SteeringDecision(action="steer", thread_id=thread_id, turn_id=thread.active_turn_id)
        if mode == "continue":
            if thread.active_turn_id:
                raise SteeringError("Thread already has an active turn; continuation would be ambiguous.")
            return SteeringDecision(action="continue", thread_id=thread_id, turn_id=None)

        if thread.active_turn_id:
            return SteeringDecision(action="steer", thread_id=thread_id, turn_id=thread.active_turn_id)
        if thread.status_type in {"idle", "notLoaded", "unknown"}:
            return SteeringDecision(action="continue", thread_id=thread_id, turn_id=None)
        raise SteeringError(f"Thread state is uncertain: {thread.status_type}")

    async def apply(self, thread_id: str, text: str, mode: str = "auto") -> SteeringDecision:
        decision = await self.decide(thread_id, mode)
        if decision.action == "steer":
            assert decision.turn_id is not None
            await self._adapter.steer_turn(thread_id, decision.turn_id, text)
            return decision
        await self._adapter.resume_thread(thread_id)
        turn_envelope = await self._adapter.start_turn(thread_id, text)
        self._ingestor.project_turn_snapshot(turn_envelope.turn.model_dump(by_alias=True), thread_id=thread_id)
        return SteeringDecision(action="continue", thread_id=thread_id, turn_id=turn_envelope.turn.id)

    async def process_queue(self, thread_id: str) -> list[QueuedInputRecord]:
        items = self._registry.list_queued_inputs(thread_id=thread_id, statuses=["queued"])
        processed: list[QueuedInputRecord] = []
        for item in items:
            submitted = item.model_copy(update={"status": "submitted", "submitted_at": utc_now_iso()})
            self._registry.save_queued_input(submitted)
            try:
                decision = await self.apply(thread_id, submitted.text, submitted.mode)
                completed = submitted.model_copy(
                    update={
                        "status": "done",
                        "completed_at": utc_now_iso(),
                        "action_taken": decision.action,
                        "turn_id": decision.turn_id,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                completed = submitted.model_copy(
                    update={
                        "status": "failed",
                        "completed_at": utc_now_iso(),
                        "error": str(exc),
                    }
                )
            self._registry.save_queued_input(completed)
            processed.append(completed)
        return processed

    async def _refresh_thread(self, thread_id: str) -> ThreadRecord:
        thread_envelope = await self._adapter.read_thread(thread_id, include_turns=True)
        thread = self._ingestor.project_thread_snapshot(thread_envelope.thread.model_dump(by_alias=True))
        if thread.active_turn_id or thread.status_type != "active":
            return thread
        raise SteeringError("Thread is active but no active turn id is known; refusing to guess.")
