from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from config import load_config
from logging_utils import configure_logging
from models import EventRecord, ThreadRecord, TurnRecord
from service import OrchestratorService
from transport import UNHANDLED

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()

ConfigOption = Annotated[Path | None, typer.Option("--config", help="Path to a TOML config file.")]
DataDirOption = Annotated[Path | None, typer.Option("--data-dir", help="Override the local registry directory.")]
ServerCmdOption = Annotated[
    str | None,
    typer.Option("--server-cmd", help="Override the App Server spawn command as a single shell-style string."),
]


def _build_service(config_path: Path | None, data_dir: Path | None, server_cmd: str | None) -> OrchestratorService:
    config = load_config(config_path, data_dir=data_dir, server_cmd=server_cmd)
    configure_logging(config.log_level)
    return OrchestratorService(config)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _execute_with_service(
    config_path: Path | None,
    data_dir: Path | None,
    server_cmd: str | None,
    operation: Callable[[OrchestratorService], Awaitable[Any]],
) -> Any:
    service = _build_service(config_path, data_dir, server_cmd)
    try:
        return await operation(service)
    finally:
        await service.close()


def _turn_timestamp(turn: TurnRecord) -> str:
    return turn.completed_at or turn.started_at or ""


def _render_threads(rows: list[ThreadRecord], turns_by_thread: dict[str, list[TurnRecord]] | None = None) -> None:
    table = Table(title="Threads")
    table.add_column("Thread ID")
    table.add_column("Name")
    table.add_column("Core Folder")
    table.add_column("Status")
    table.add_column("Last Turn")
    table.add_column("Active Turn")
    table.add_column("Archived")
    for row in rows:
        thread_turns = (turns_by_thread or {}).get(row.thread_id, [])
        last_turn = _turn_timestamp(thread_turns[0]) if thread_turns else ""
        table.add_row(
            row.thread_id,
            row.name or "",
            row.cwd or "",
            row.status_type,
            last_turn,
            row.active_turn_id or "",
            "yes" if row.archived else "no",
        )
    console.print(table)


def _render_turns(turns: list[TurnRecord]) -> None:
    table = Table(title="Turns")
    table.add_column("Turn ID")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Completed")
    table.add_column("Summary")
    for turn in turns:
        table.add_row(turn.turn_id, turn.status, turn.started_at or "", turn.completed_at or "", turn.summary or "")
    console.print(table)


def _extract_message_entries(turn: TurnRecord) -> list[tuple[str, str]]:
    return _extract_message_entries_from_payload(turn.raw_turn, fallback_turn_id=turn.turn_id)


def _message_key(turn_id: str, role: str, text: str) -> str:
    return f"{turn_id}:{role}:{text}"


def _extract_message_entries_from_payload(turn_payload: dict[str, Any], *, fallback_turn_id: str = "") -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    turn_id = str(turn_payload.get("id") or fallback_turn_id)
    for item in turn_payload.get("items", []):
        item_type = item.get("type")
        if item_type == "userMessage":
            content = item.get("content", [])
            text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"]
            text = "".join(text_parts).strip()
            if text:
                messages.append((_message_key(turn_id, "user", text), f"user: {text}"))
        elif item_type == "agentMessage":
            text = (item.get("text") or "").strip()
            if text:
                phase = item.get("phase")
                prefix = "assistant"
                if phase:
                    prefix = f"assistant/{phase}"
                messages.append((_message_key(turn_id, "assistant", text), f"{prefix}: {text}"))
    return messages


def _extract_live_message_entry(event: EventRecord) -> tuple[str, str] | None:
    payload = event.payload_json
    item_payload = payload.get("item")
    if isinstance(item_payload, dict):
        item_type = str(item_payload.get("type") or "")
        if item_type in {"userMessage", "agentMessage"}:
            entries = _extract_message_entries_from_payload(
                {"id": event.turn_id or "", "items": [item_payload]},
                fallback_turn_id=event.turn_id or "",
            )
            if entries:
                return entries[0]

    return None


def _available_approval_decisions(params: dict[str, Any]) -> list[Any]:
    return list(params.get("availableDecisions") or params.get("available_decisions") or [])


def _approval_help_text(params: dict[str, Any]) -> str:
    decisions = _available_approval_decisions(params)
    labels: list[str] = []
    for decision in decisions:
        if decision == "accept":
            labels.append("approve")
        elif decision == "acceptForSession":
            labels.append("approve-session")
        elif decision == "decline":
            labels.append("decline")
        elif decision == "cancel":
            labels.append("cancel")
        elif isinstance(decision, dict) and "acceptWithExecpolicyAmendment" in decision:
            labels.append("approve-amend")
    if not labels:
        labels = ["approve", "cancel"]
    return ", ".join(labels)


def _parse_approval_input(text: str, params: dict[str, Any]) -> Any | None:
    normalized = text.strip().lower()
    decisions = _available_approval_decisions(params)
    proposed_amendment = params.get("proposedExecpolicyAmendment") or params.get("proposed_execpolicy_amendment")

    if normalized in {"approve", "/approve", "y", "yes"} and "accept" in decisions:
        return {"decision": "accept"}
    if normalized in {"approve-session", "/approve-session"} and "acceptForSession" in decisions:
        return {"decision": "acceptForSession"}
    if normalized in {"approve-amend", "/approve-amend"} and proposed_amendment:
        return {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": list(proposed_amendment)}}}
    if normalized in {"decline", "/decline", "n", "no"} and "decline" in decisions:
        return {"decision": "decline"}
    if normalized in {"cancel", "/cancel"} and "cancel" in decisions:
        return {"decision": "cancel"}

    if not decisions and normalized in {"approve", "/approve", "y", "yes"}:
        return {"decision": "accept"}
    if not decisions and normalized in {"cancel", "/cancel", "decline", "/decline", "n", "no"}:
        return {"decision": "cancel"}
    return None


@app.command(help="List known threads from the App Server and refresh the local registry cache.")
def threads(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        rows = await service.list_threads()
        turns_by_thread = {row.thread_id: service.registry.list_turns(thread_id=row.thread_id) for row in rows}
        _render_threads(rows, turns_by_thread)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Show locally cached metadata and recent turns for a thread.")
def inspect(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    service = _build_service(config, data_dir, server_cmd)
    thread, turns = service.inspect_local(thread_id)
    if thread is None:
        raise typer.Exit(code=1)
    console.print(f"Thread: {thread.thread_id}")
    console.print(f"Name: {thread.name or ''}")
    console.print(f"Status: {thread.status_type}")
    console.print(f"Active turn: {thread.active_turn_id or ''}")
    _render_turns(turns[:10])


@app.command(help="Read a thread from the App Server and refresh its stored local snapshot.")
def read(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.read_thread(thread_id, include_turns=True)
        _render_threads([thread])
        _render_turns(service.registry.list_turns(thread_id=thread_id))

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Replay recent thread messages, then resume the thread and print newly detected live messages.")
def listen(
    thread_id: str,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop after this many events.")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Skip replaying recent thread messages before listening.")] = False,
    history_limit: Annotated[int, typer.Option("--history-limit", help="Number of most recent messages to replay before listening.")] = 20,
    refresh_seconds: Annotated[
        float,
        typer.Option("--refresh-seconds", help="Fallback snapshot refresh interval while listening."),
    ] = 2.0,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        console.print(f"Listening on thread {thread_id}")
        seen_message_ids: set[str] = set()

        async def collect_messages() -> list[tuple[str, str]]:
            thread = await service.read_thread(thread_id, include_turns=True)
            messages: list[tuple[str, str]] = []
            raw_turns = thread.raw_thread.get("turns", [])
            if isinstance(raw_turns, list) and raw_turns:
                for turn_payload in raw_turns:
                    if isinstance(turn_payload, dict):
                        messages.extend(_extract_message_entries_from_payload(turn_payload))
                return messages
            turns = list(reversed(service.registry.list_turns(thread_id=thread_id)))
            for turn in turns:
                messages.extend(_extract_message_entries(turn))
            return messages

        async def sync_messages(*, print_history: bool) -> int:
            printed = 0
            messages = await collect_messages()
            already_seen = set(seen_message_ids)
            if print_history and history_limit > 0:
                for message_id, _ in messages:
                    seen_message_ids.add(message_id)
                for message_id, text in messages[-history_limit:]:
                    if message_id in already_seen:
                        continue
                    console.print(text)
                    printed += 1
                return printed
            for message_id, text in messages:
                if message_id in seen_message_ids:
                    continue
                console.print(text)
                seen_message_ids.add(message_id)
                printed += 1
            return printed

        if not no_history:
            await sync_messages(print_history=True)
        else:
            messages = await collect_messages()
            for message_id, _ in messages:
                seen_message_ids.add(message_id)

        stop_refresh = asyncio.Event()

        async def refresh_loop() -> None:
            while not stop_refresh.is_set():
                await asyncio.sleep(refresh_seconds)
                if stop_refresh.is_set():
                    break
                await sync_messages(print_history=False)

        async def on_event(event: EventRecord) -> None:
            live_message = _extract_live_message_entry(event)
            if live_message is not None:
                message_id, text = live_message
                if message_id not in seen_message_ids:
                    console.print(text)
                    seen_message_ids.add(message_id)
                return
            await sync_messages(print_history=False)

        refresh_task = asyncio.create_task(refresh_loop())
        try:
            await service.listen(
                thread_id,
                on_event,
                max_events=max_events,
            )
        finally:
            stop_refresh.set()
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(name="listen-and-send", help="Listen to a thread and start a new turn for each line typed into the terminal.")
def listen_and_send(
    thread_id: str,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop after this many live events.")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Skip replaying recent thread messages before listening.")] = False,
    history_limit: Annotated[int, typer.Option("--history-limit", help="Number of most recent messages to replay before listening.")] = 20,
    refresh_seconds: Annotated[
        float,
        typer.Option("--refresh-seconds", help="Fallback snapshot refresh interval while listening."),
    ] = 2.0,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        console.print(f"Listening and sending on thread {thread_id}")
        console.print("Type a line and press Enter to start a new turn. Send EOF to exit.")
        seen_message_ids: set[str] = set()
        approval_queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = asyncio.Queue()
        active_approval: tuple[dict[str, Any], asyncio.Future[dict[str, Any]]] | None = None

        async def collect_messages() -> list[tuple[str, str]]:
            thread = await service.read_thread(thread_id, include_turns=True)
            messages: list[tuple[str, str]] = []
            raw_turns = thread.raw_thread.get("turns", [])
            if isinstance(raw_turns, list) and raw_turns:
                for turn_payload in raw_turns:
                    if isinstance(turn_payload, dict):
                        messages.extend(_extract_message_entries_from_payload(turn_payload))
                return messages
            turns = list(reversed(service.registry.list_turns(thread_id=thread_id)))
            for turn in turns:
                messages.extend(_extract_message_entries(turn))
            return messages

        async def sync_messages(*, print_history: bool) -> int:
            printed = 0
            messages = await collect_messages()
            already_seen = set(seen_message_ids)
            if print_history and history_limit > 0:
                for message_id, _ in messages:
                    seen_message_ids.add(message_id)
                for message_id, text in messages[-history_limit:]:
                    if message_id in already_seen:
                        continue
                    console.print(text)
                    printed += 1
                return printed
            for message_id, text in messages:
                if message_id in seen_message_ids:
                    continue
                console.print(text)
                seen_message_ids.add(message_id)
                printed += 1
            return printed

        async def send_prompt_to_thread(prompt: str) -> tuple[str, str]:
            turn = await service.start_turn_on_thread(thread_id, prompt)
            return turn.turn_id, f"Started turn {turn.turn_id}"

        async def handle_server_request(method: str, params: dict[str, Any]) -> Any:
            if method != "item/commandExecution/requestApproval":
                return UNHANDLED
            loop = asyncio.get_running_loop()
            response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
            await approval_queue.put((params, response_future))
            return await response_future

        service.transport.add_request_handler(handle_server_request)

        if not no_history:
            await sync_messages(print_history=True)
        else:
            messages = await collect_messages()
            for message_id, _ in messages:
                seen_message_ids.add(message_id)

        stop_refresh = asyncio.Event()
        input_busy = asyncio.Event()

        async def approval_loop() -> None:
            nonlocal active_approval
            while True:
                active_approval = await approval_queue.get()
                params, response_future = active_approval
                reason = params.get("reason") or "Approval requested."
                command = params.get("command") or ""
                console.print(f"approval: {reason}")
                if command:
                    console.print(f"command: {command}")
                console.print(f"reply with: {_approval_help_text(params)}")
                try:
                    await asyncio.shield(response_future)
                finally:
                    active_approval = None

        async def refresh_loop() -> None:
            while not stop_refresh.is_set():
                await asyncio.sleep(refresh_seconds)
                if stop_refresh.is_set():
                    break
                await sync_messages(print_history=False)

        async def on_event(event: EventRecord) -> None:
            live_message = _extract_live_message_entry(event)
            if live_message is not None:
                message_id, text = live_message
                if message_id not in seen_message_ids:
                    console.print(text)
                    seen_message_ids.add(message_id)
                return
            await sync_messages(print_history=False)

        async def input_loop() -> None:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if line == "":
                    return
                prompt = line.strip()
                if not prompt:
                    continue
                if active_approval is not None:
                    params, response_future = active_approval
                    decision = _parse_approval_input(prompt, params)
                    if decision is None:
                        console.print(f"invalid approval response; reply with: {_approval_help_text(params)}")
                        continue
                    response_future.set_result(decision)
                    console.print(f"approval sent: {decision['decision']}")
                    continue
                try:
                    input_busy.set()
                    accepted_turn_id, action_label = await send_prompt_to_thread(prompt)
                    await sync_messages(print_history=False)
                    user_message_id = _message_key(accepted_turn_id, "user", prompt)
                    if user_message_id not in seen_message_ids:
                        seen_message_ids.add(user_message_id)
                        console.print(f"user: {prompt}")
                    console.print(action_label)
                except Exception as exc:  # noqa: BLE001
                    console.print(f"Send failed: {exc}")
                    continue
                finally:
                    input_busy.clear()

        refresh_task = asyncio.create_task(refresh_loop())
        approval_task = asyncio.create_task(approval_loop())
        listener_task = asyncio.create_task(
            service.listen(
                thread_id,
                on_event,
                max_events=max_events,
            )
        )
        input_task = asyncio.create_task(input_loop())
        try:
            done, pending = await asyncio.wait(
                {listener_task, input_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if input_task in done and listener_task in pending:
                with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(listener_task), timeout=0.5)
                if not listener_task.done():
                    listener_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await listener_task
            elif listener_task in done and input_task in pending:
                if input_busy.is_set():
                    with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                        await asyncio.wait_for(asyncio.shield(input_task), timeout=1)
                if not input_task.done():
                    input_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await input_task
            for task in done:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            stop_refresh.set()
            refresh_task.cancel()
            approval_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
            with contextlib.suppress(asyncio.CancelledError):
                await approval_task

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Validate local configuration and test connectivity to the Codex App Server.")
def doctor(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        report = await service.doctor()
        table = Table(title="Doctor")
        table.add_column("Check")
        table.add_column("OK")
        table.add_column("Details")
        for name, result in report.items():
            details = ", ".join(f"{key}={value}" for key, value in result.items() if key != "ok")
            table.add_row(name, "yes" if result.get("ok") else "no", details)
        console.print(table)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))
