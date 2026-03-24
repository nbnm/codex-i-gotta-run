from __future__ import annotations

import asyncio
import json
import logging
from asyncio.subprocess import PIPE, Process
from collections.abc import Awaitable, Callable
from typing import Any

from codex_thread_orchestrator.models import JsonRpcErrorPayload

logger = logging.getLogger(__name__)

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class TransportError(RuntimeError):
    """Raised when the JSON-RPC transport fails."""


class JsonRpcError(RuntimeError):
    """Raised when the server returns a JSON-RPC error."""

    def __init__(self, payload: JsonRpcErrorPayload):
        super().__init__(f"{payload.code}: {payload.message}")
        self.payload = payload


class StdioJsonRpcTransport:
    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._process: Process | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()
        self._notification_handlers: list[NotificationHandler] = []
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    async def connect(self) -> None:
        if self._process is not None:
            return
        if not self._command:
            raise TransportError("App Server command is not configured.")
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=self._cwd,
            stdin=PIPE,
            stdout=PIPE,
            stderr=PIPE,
        )
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            raise TransportError("Failed to open stdio pipes to App Server.")
        self._stdout_task = asyncio.create_task(self._read_stdout_loop())
        self._stderr_task = asyncio.create_task(self._read_stderr_loop())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending_values = list(self._pending.values())
        self._pending.clear()
        for future in pending_values:
            if not future.done():
                future.set_exception(TransportError("Transport closed."))
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    self._process.kill()
                    await self._process.wait()
        self._process = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._process is None:
            await self.connect()
        request_id = self._request_id
        self._request_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        return await future

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._process is None:
            await self.connect()
        await self._send({"method": method, "params": params or {}})

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise TransportError("Transport is not connected.")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._send_lock:
            self._process.stdin.write(encoded)
            await self._process.stdin.drain()

    async def _read_stdout_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    if self._closed:
                        return
                    raise TransportError("App Server stdout closed.")
                raw = line.decode("utf-8").strip()
                if not raw:
                    continue
                message = json.loads(raw)
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Transport stdout loop failed", extra={"error": str(exc)})
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(exc)
            self._pending.clear()

    async def _read_stderr_loop(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    return
                logger.warning("app_server_stderr", extra={"line": line.decode("utf-8", errors="replace").rstrip()})
        except asyncio.CancelledError:
            raise

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            request_id = int(message["id"])
            future = self._pending.pop(request_id, None)
            if future is None:
                return
            if "error" in message:
                future.set_exception(JsonRpcError(JsonRpcErrorPayload.model_validate(message["error"])))
            else:
                future.set_result(message.get("result"))
            return

        if "method" in message and "id" not in message:
            method = str(message["method"])
            params = message.get("params") or {}
            for handler in list(self._notification_handlers):
                result = handler(method, params)
                if asyncio.iscoroutine(result):
                    await result
            return

        logger.warning("Unhandled JSON-RPC message", extra={"message": message})
