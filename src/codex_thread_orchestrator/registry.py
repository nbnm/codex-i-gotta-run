from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel

from codex_thread_orchestrator.models import ConnectionState, EventRecord, QueuedInputRecord, ThreadRecord, TurnRecord

T = TypeVar("T", bound=BaseModel)


class JsonRegistry:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.threads_dir = self.data_dir / "threads"
        self.turns_dir = self.data_dir / "turns"
        self.queue_dir = self.data_dir / "queued_inputs"
        self.events_path = self.data_dir / "events.jsonl"
        self.connection_state_path = self.data_dir / "connection_state.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.turns_dir.mkdir(parents=True, exist_ok=True)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.events_path.touch(exist_ok=True)

    def save_thread(self, thread: ThreadRecord) -> None:
        self._atomic_write(self.threads_dir / f"{thread.thread_id}.json", thread.model_dump(mode="json"))

    def get_thread(self, thread_id: str) -> ThreadRecord | None:
        return self._load_model(self.threads_dir / f"{thread_id}.json", ThreadRecord)

    def list_threads(self) -> list[ThreadRecord]:
        items = self._load_collection(self.threads_dir, ThreadRecord)
        return sorted(items, key=lambda item: (item.updated_at or 0, item.last_seen_at), reverse=True)

    def save_turn(self, turn: TurnRecord) -> None:
        self._atomic_write(self.turns_dir / f"{turn.turn_id}.json", turn.model_dump(mode="json"))

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        return self._load_model(self.turns_dir / f"{turn_id}.json", TurnRecord)

    def list_turns(self, *, thread_id: str | None = None) -> list[TurnRecord]:
        turns = self._load_collection(self.turns_dir, TurnRecord)
        if thread_id is not None:
            turns = [turn for turn in turns if turn.thread_id == thread_id]
        return sorted(turns, key=lambda item: item.started_at or "", reverse=True)

    def append_event(self, event: EventRecord) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json())
            handle.write("\n")

    def list_events(self, *, thread_id: str | None = None, limit: int | None = None) -> list[EventRecord]:
        items: list[EventRecord] = []
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                event = EventRecord.model_validate_json(raw)
                if thread_id is not None and event.thread_id != thread_id:
                    continue
                items.append(event)
        if limit is not None:
            items = items[-limit:]
        return items

    def save_queued_input(self, item: QueuedInputRecord) -> None:
        self._atomic_write(self.queue_dir / f"{item.id}.json", item.model_dump(mode="json"))

    def get_queued_input(self, item_id: str) -> QueuedInputRecord | None:
        return self._load_model(self.queue_dir / f"{item_id}.json", QueuedInputRecord)

    def list_queued_inputs(
        self,
        *,
        thread_id: str | None = None,
        statuses: Iterable[str] | None = None,
    ) -> list[QueuedInputRecord]:
        items = self._load_collection(self.queue_dir, QueuedInputRecord)
        if thread_id is not None:
            items = [item for item in items if item.thread_id == thread_id]
        if statuses is not None:
            allowed = set(statuses)
            items = [item for item in items if item.status in allowed]
        return sorted(items, key=lambda item: item.created_at)

    def save_connection_state(self, state: ConnectionState) -> None:
        self._atomic_write(self.connection_state_path, state.model_dump(mode="json"))

    def get_connection_state(self) -> ConnectionState | None:
        return self._load_model(self.connection_state_path, ConnectionState)

    def _atomic_write(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        temp_path.replace(path)

    def _load_model(self, path: Path, model_type: type[T]) -> T | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return model_type.model_validate(json.load(handle))

    def _load_collection(self, directory: Path, model_type: type[T]) -> list[T]:
        items: list[T] = []
        for path in sorted(directory.glob("*.json")):
            loaded = self._load_model(path, model_type)
            if loaded is not None:
                items.append(loaded)
        return items

