from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from codex_thread_orchestrator.config import load_config
from codex_thread_orchestrator.logging_utils import configure_logging
from codex_thread_orchestrator.models import EventRecord, ThreadRecord, TurnRecord
from codex_thread_orchestrator.service import OrchestratorService

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


def _render_threads(rows: list[ThreadRecord]) -> None:
    table = Table(title="Threads")
    table.add_column("Thread ID")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Active Turn")
    table.add_column("Archived")
    for row in rows:
        table.add_row(row.thread_id, row.name or "", row.status_type, row.active_turn_id or "", "yes" if row.archived else "no")
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


@app.command()
def connect(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        state = await service.connect()
        console.print(
            f"Connected to {state.app_server_instance or '<unset>'} "
            f"({state.platform_os or 'unknown OS'}, {state.user_agent or 'unknown UA'})"
        )

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command()
def threads(config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        rows = await service.list_threads()
        _render_threads(rows)

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command()
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


@app.command()
def read(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.read_thread(thread_id, include_turns=True)
        _render_threads([thread])
        _render_turns(service.registry.list_turns(thread_id=thread_id))

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command()
def resume(thread_id: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.resume_thread(thread_id)
        console.print(f"Resumed thread {thread.thread_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command()
def start(prompt: str, config: ConfigOption = None, data_dir: DataDirOption = None, server_cmd: ServerCmdOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        result = await service.start_new_thread(prompt)
        console.print(f"Started thread {result.thread.thread_id} turn {result.turn.turn_id}")

    _run(_execute_with_service(config, data_dir, server_cmd, operation))


@app.command(name="continue")
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


@app.command()
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


@app.command()
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


@app.command()
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


@app.command()
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


@app.command()
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


@app.command()
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


@app.command()
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
