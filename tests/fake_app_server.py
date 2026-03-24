from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


STATE_PATH = Path(os.environ["FAKE_APP_SERVER_STATE_PATH"])


def utc_ts() -> int:
    return int(time.time())


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "threads": {},
            "turns": {},
            "loaded": [],
            "counter": 1,
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def next_id(state: dict[str, Any], prefix: str) -> str:
    value = state["counter"]
    state["counter"] += 1
    return f"{prefix}_{value}"


def serialize_thread(state: dict[str, Any], thread: dict[str, Any], *, include_turns: bool = False) -> dict[str, Any]:
    payload = dict(thread)
    if include_turns:
        payload["turns"] = [dict(state["turns"][turn_id]) for turn_id in thread.get("turns", [])]
    else:
        payload.pop("turns", None)
    return payload


def emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def error_response(request_id: int, code: int, message: str) -> dict[str, Any]:
    return {"id": request_id, "error": {"code": code, "message": message}}


def handle_request(state: dict[str, Any], message: dict[str, Any], initialized: bool) -> tuple[dict[str, Any] | None, bool]:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return {
            "id": request_id,
            "result": {
                "userAgent": "fake-app-server/0.1",
                "platformFamily": "test",
                "platformOs": "linux",
            },
        }, True

    if not initialized:
        return error_response(request_id, -32002, "Not initialized"), initialized

    if method == "thread/start":
        thread_id = next_id(state, "thr")
        thread = {
            "id": thread_id,
            "name": None,
            "preview": "",
            "createdAt": utc_ts(),
            "updatedAt": utc_ts(),
            "archived": False,
            "status": {"type": "idle"},
            "turns": [],
        }
        state["threads"][thread_id] = thread
        state["loaded"].append(thread_id)
        save_state(state)
        payload = serialize_thread(state, thread)
        emit({"method": "thread/started", "params": {"thread": payload}})
        return {"id": request_id, "result": {"thread": payload}}, initialized

    if method == "thread/list":
        threads = [serialize_thread(state, thread) for thread in state["threads"].values()]
        threads.sort(key=lambda item: item.get("createdAt", 0), reverse=True)
        return {"id": request_id, "result": {"data": threads, "nextCursor": None}}, initialized

    if method == "thread/read":
        thread = state["threads"].get(params["threadId"])
        if thread is None:
            return error_response(request_id, 404, "Unknown thread"), initialized
        thread_copy = serialize_thread(state, thread, include_turns=bool(params.get("includeTurns")))
        return {"id": request_id, "result": {"thread": thread_copy}}, initialized

    if method == "thread/resume":
        thread = state["threads"].get(params["threadId"])
        if thread is None:
            return error_response(request_id, 404, "Unknown thread"), initialized
        if thread["id"] not in state["loaded"]:
            state["loaded"].append(thread["id"])
        thread["status"] = thread.get("status", {"type": "idle"})
        save_state(state)
        payload = serialize_thread(state, thread)
        emit({"method": "thread/started", "params": {"thread": payload}})
        return {"id": request_id, "result": {"thread": payload}}, initialized

    if method == "thread/unsubscribe":
        thread_id = params["threadId"]
        if thread_id not in state["threads"]:
            return {"id": request_id, "result": {"status": "notLoaded"}}, initialized
        if thread_id in state["loaded"]:
            state["loaded"].remove(thread_id)
            save_state(state)
            emit({"method": "thread/status/changed", "params": {"threadId": thread_id, "status": {"type": "notLoaded"}}})
            emit({"method": "thread/closed", "params": {"threadId": thread_id}})
            return {"id": request_id, "result": {"status": "unsubscribed"}}, initialized
        return {"id": request_id, "result": {"status": "notSubscribed"}}, initialized

    if method == "turn/start":
        thread = state["threads"].get(params["threadId"])
        if thread is None:
            return error_response(request_id, 404, "Unknown thread"), initialized
        turn_id = next_id(state, "turn")
        text = params["input"][0]["text"]
        hold = "hold" in text.lower()
        turn = {
            "id": turn_id,
            "threadId": thread["id"],
            "status": "inProgress",
            "items": [],
            "error": None,
            "summary": None,
        }
        state["turns"][turn_id] = turn
        thread.setdefault("turns", []).append(turn_id)
        thread["status"] = {"type": "active", "activeFlags": []}
        thread["activeTurnId"] = turn_id
        thread["updatedAt"] = utc_ts()
        save_state(state)
        emit({"method": "thread/status/changed", "params": {"threadId": thread["id"], "status": thread["status"]}})
        emit({"method": "turn/started", "params": {"threadId": thread["id"], "turn": turn}})
        emit(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": thread["id"], "turnId": turn_id, "delta": f"working on: {text}"},
            }
        )
        if not hold:
            turn["status"] = "completed"
            turn["summary"] = f"Completed: {text}"
            thread["status"] = {"type": "idle"}
            thread["activeTurnId"] = None
            save_state(state)
            emit({"method": "turn/completed", "params": {"threadId": thread["id"], "turn": turn}})
            emit({"method": "thread/status/changed", "params": {"threadId": thread["id"], "status": thread["status"]}})
        return {"id": request_id, "result": {"turn": turn}}, initialized

    if method == "turn/steer":
        thread = state["threads"].get(params["threadId"])
        if thread is None:
            return error_response(request_id, 404, "Unknown thread"), initialized
        expected_turn_id = params["expectedTurnId"]
        if thread.get("activeTurnId") != expected_turn_id:
            return error_response(request_id, 409, "expectedTurnId mismatch"), initialized
        turn = state["turns"][expected_turn_id]
        steer_text = params["input"][0]["text"]
        emit(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": thread["id"], "turnId": expected_turn_id, "delta": f"steer: {steer_text}"},
            }
        )
        if "finish" in steer_text.lower():
            turn["status"] = "completed"
            turn["summary"] = f"Completed after steer: {steer_text}"
            thread["status"] = {"type": "idle"}
            thread["activeTurnId"] = None
            save_state(state)
            emit({"method": "turn/completed", "params": {"threadId": thread["id"], "turn": turn}})
            emit({"method": "thread/status/changed", "params": {"threadId": thread["id"], "status": thread["status"]}})
        return {"id": request_id, "result": {"turnId": expected_turn_id}}, initialized

    if method == "turn/interrupt":
        thread = state["threads"].get(params["threadId"])
        if thread is None:
            return error_response(request_id, 404, "Unknown thread"), initialized
        turn = state["turns"].get(params["turnId"])
        if turn is None:
            return error_response(request_id, 404, "Unknown turn"), initialized
        turn["status"] = "interrupted"
        thread["status"] = {"type": "idle"}
        thread["activeTurnId"] = None
        save_state(state)
        emit({"method": "turn/completed", "params": {"threadId": thread["id"], "turn": turn}})
        emit({"method": "thread/status/changed", "params": {"threadId": thread["id"], "status": thread["status"]}})
        return {"id": request_id, "result": {}}, initialized

    return error_response(request_id, -32601, f"Unknown method {method}"), initialized


def main() -> None:
    initialized = False
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        message = json.loads(raw)
        if "id" in message:
            response, initialized = handle_request(state, message, initialized)
            if response is not None:
                emit(response)
        elif message.get("method") == "initialized":
            initialized = True


state = load_state()


if __name__ == "__main__":
    main()
