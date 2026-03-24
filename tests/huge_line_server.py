from __future__ import annotations

import json
import sys
from typing import Any


LARGE_TEXT = "x" * 200_000


def emit(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    initialized = False
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        message = json.loads(raw)
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            emit(
                {
                    "id": request_id,
                    "result": {
                        "userAgent": "huge-line-server/0.1",
                        "platformFamily": "test",
                        "platformOs": "linux",
                    },
                }
            )
            continue

        if method == "initialized":
            initialized = True
            continue

        if not initialized:
            emit({"id": request_id, "error": {"code": -32002, "message": "Not initialized"}})
            continue

        if method == "thread/resume":
            thread_id = message["params"]["threadId"]
            thread = {
                "id": thread_id,
                "name": "huge-thread",
                "preview": LARGE_TEXT,
                "cwd": "/tmp/huge-thread",
                "createdAt": 1,
                "updatedAt": 2,
                "archived": False,
                "status": {"type": "idle"},
            }
            emit({"method": "thread/started", "params": {"thread": thread}})
            emit({"id": request_id, "result": {"thread": thread}})
            continue

        emit({"id": request_id, "error": {"code": -32601, "message": f"Unknown method {method}"}})


if __name__ == "__main__":
    main()
