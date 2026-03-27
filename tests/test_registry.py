from __future__ import annotations

from pathlib import Path

from models import EventRecord, TelegramSessionRecord, ThreadRecord, TurnRecord
from registry import JsonRegistry


def test_registry_round_trip(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    thread = ThreadRecord(thread_id="thr_1", status_type="idle")
    turn = TurnRecord(turn_id="turn_1", thread_id="thr_1", status="completed")
    registry.save_thread(thread)
    registry.save_turn(turn)
    registry.append_event(EventRecord(id="evt_1", thread_id="thr_1", turn_id="turn_1", event_type="turn/completed", payload_json={"turnId": "turn_1"}))

    loaded_thread = registry.get_thread("thr_1")
    loaded_turn = registry.get_turn("turn_1")
    events = registry.list_events(thread_id="thr_1")

    assert loaded_thread is not None
    assert loaded_thread.thread_id == "thr_1"
    assert loaded_turn is not None
    assert loaded_turn.turn_id == "turn_1"
    assert events[-1].event_type == "turn/completed"


def test_registry_persists_telegram_session(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    session = TelegramSessionRecord(thread_id="thr_1", chat_id=777, last_update_id=42)

    registry.save_telegram_session(session)
    loaded = registry.get_telegram_session("thr_1")

    assert loaded is not None
    assert loaded.thread_id == "thr_1"
    assert loaded.chat_id == 777
    assert loaded.last_update_id == 42


def test_registry_deletes_telegram_session(tmp_path: Path) -> None:
    registry = JsonRegistry(tmp_path / "registry")
    session = TelegramSessionRecord(thread_id="thr_1", chat_id=777)

    registry.save_telegram_session(session)
    registry.delete_telegram_session("thr_1")

    assert registry.get_telegram_session("thr_1") is None
