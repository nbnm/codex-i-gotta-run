from __future__ import annotations

import asyncio
import json
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


def _render_events(events: list[EventRecord]) -> None:
    table = Table(title="Events")
    table.add_column("Received At")
    table.add_column("Type")
    table.add_column("Thread")
    table.add_column("Turn")
    for event in events:
        table.add_row(event.received_at, event.event_type, event.thread_id or "", event.turn_id or "")
    console.print(table)


def _format_live_event(event: EventRecord) -> str:
    payload = event.payload_json
    if event.event_type == "item/agentMessage/delta" and "delta" in payload:
        return f"[{event.received_at}] agent_message: {payload['delta']}"
    if event.event_type == "turn/completed" and isinstance(payload.get("turn"), dict):
        summary = payload["turn"].get("summary") or ""
        return f"[{event.received_at}] turn/completed {event.turn_id or ''} {summary}".strip()
    if event.event_type == "turn/started" and isinstance(payload.get("turn"), dict):
        return f"[{event.received_at}] turn/started {payload['turn'].get('id', '')}".strip()
    return f"[{event.received_at}] {event.event_type} {json.dumps(payload, default=str, ensure_ascii=True)}"


@app.command(help="Connect to the local Codex App Server and initialize the client session.")
def connect(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        state = await service.connect()
        console.print(
            f"Connected to {state.app_server_instance or '<unset>'} "
            f"({state.platform_os or 'unknown OS'}, {state.user_agent or 'unknown UA'})"
        )

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="List known threads from the App Server and refresh the local registry cache.")
def threads(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        rows = await service.list_threads()
        turns_by_thread = {row.thread_id: service.registry.list_turns(thread_id=row.thread_id) for row in rows}
        _render_threads(rows, turns_by_thread)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Show locally cached metadata, recent turns, and queued inputs for a thread.")
def inspect(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    service = _build_service(config, data_dir, server_cmd)
    thread, turns, queue_items = service.inspect_local(thread_id)
    if thread is None:
        raise typer.Exit(code=1)
    console.print(f"Thread: {thread.thread_id}")
    console.print(f"Name: {thread.name or ''}")
    console.print(f"Status: {thread.status_type}")
    console.print(f"Active turn: {thread.active_turn_id or ''}")
    _render_turns(turns[:10])
    if queue_items:
        console.print(f"Queued inputs: {len(queue_items)}")


@app.command(help="Read a thread from the App Server and refresh its stored local snapshot.")
def read(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.read_thread(thread_id, include_turns=True)
        _render_threads([thread])
        _render_turns(service.registry.list_turns(thread_id=thread_id))

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Resume a known thread so it becomes active on the App Server.")
def resume(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.resume_thread(thread_id)
        console.print(f"Resumed thread {thread.thread_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Create a new thread and start its first turn with the provided prompt.")
def start(prompt: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        result = await service.start_new_thread(prompt)
        console.print(f"Started thread {result.thread.thread_id} turn {result.turn.turn_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(name="continue", help="Start the next turn on an existing thread after refreshing its current state.")
def continue_(
    thread_id: str,
    prompt: str,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        decision = await service.continue_thread(thread_id, prompt)
        console.print(f"{decision.action} -> {decision.turn_id or '<new turn pending>'}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Append more user input to an active in-flight turn with a known turn ID.")
def steer(
    thread_id: str,
    turn_id: str,
    prompt: str,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        accepted_turn_id = await service.steer_thread(thread_id, turn_id, prompt)
        console.print(f"Steered turn {accepted_turn_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Interrupt the currently active turn for a thread.")
def interrupt(
    thread_id: str,
    turn_id: str,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        await service.interrupt_turn(thread_id, turn_id)
        console.print(f"Interrupted {turn_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Resume a thread and display its recent live events.")
def tail(
    thread_id: str,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop after this many events.")] = None,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        events = await service.tail_events(thread_id, max_events=max_events)
        _render_events(events)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Replay recent thread events, then resume the thread and print live events as they arrive.")
def listen(
    thread_id: str,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop after this many events.")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Skip replaying recent known events before listening.")] = False,
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        console.print(f"Listening on thread {thread_id}")
        await service.listen(
            thread_id,
            lambda event: console.print(_format_live_event(event)),
            max_events=max_events,
            include_history=not no_history,
        )

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Persist a follow-up prompt in the local queue for later steering or continuation.")
def queue(
    thread_id: str,
    prompt: str,
    mode: Annotated[str, typer.Option("--mode", help="Queue mode: steer, continue, or auto.")] = "auto",
    config: ConfigOption = None,
    data_dir: DataDirOption = None,
    server_cmd: ServerCmdOption = None,
) -> None:
    service = _build_service(config, data_dir, server_cmd)
    item = service.queue_input(thread_id, prompt, mode=mode)
    console.print(f"Queued {item.id} for thread {thread_id}")


@app.command(help="Process queued inputs for a thread using the v0 auto-steering rules.")
def autosteer(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        processed = await service.process_queue(thread_id)
        table = Table(title="Autosteer")
        table.add_column("Queue ID")
        table.add_column("Status")
        table.add_column("Action")
        table.add_column("Turn ID")
        table.add_column("Error")
        for item in processed:
            table.add_row(item.id, item.status, item.action_taken or "", item.turn_id or "", item.error or "")
        console.print(table)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(help="Show local connection state, active turns, and queued input counts.")
def status(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    service = _build_service(config, data_dir, server_cmd)
    snapshot = service.status_snapshot()
    connection = snapshot["connection"]
    if connection is not None:
        console.print(f"Connection initialized at: {connection.initialized_at}")
        console.print(f"Last event at: {connection.last_event_at or ''}")
        console.print(f"Last error: {connection.last_error or ''}")
    _render_threads(snapshot["active_threads"])
    console.print(f"Queued inputs: {len(snapshot['queued_inputs'])}")


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
