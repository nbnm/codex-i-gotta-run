from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from config import load_config
from logging_utils import configure_logging
from models import EventRecord, ThreadRecord, TurnRecord
from service import OrchestratorService
from telegram_integration import HttpTelegramBotApi, TelegramBridgeHub, TelegramOperatorBridge, build_topic_name
from transport import UNHANDLED

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
except ImportError:  # pragma: no cover - dependency is expected in normal runtime
    PromptSession = None
    patch_stdout = None

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()
PROMPT_TEXT = "> "

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Path to a TOML config file. Defaults to ./config.toml when present."),
]


class OperatorInterface(str, Enum):
    CLI = "cli"
    TELEGRAM = "telegram"


def _build_service(config_path: Path | None) -> OrchestratorService:
    config = load_config(config_path)
    configure_logging(config.log_level)
    return OrchestratorService(config)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _execute_with_service(
    config_path: Path | None,
    operation: Callable[[OrchestratorService], Awaitable[Any]],
) -> Any:
    service = _build_service(config_path)
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
    labels = _approval_button_labels(params)
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


def _approval_button_labels(params: dict[str, Any]) -> list[str]:
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
    return labels


async def _emit_output(text: str, telegram_bridge: TelegramOperatorBridge | None) -> None:
    console.print(text)
    if telegram_bridge is not None:
        await telegram_bridge.send_text(text)


def _active_thread_sort_key(thread: ThreadRecord) -> tuple[float, str]:
    updated_at = float(thread.updated_at or 0)
    return (updated_at, thread.last_seen_at)


def _is_active_thread(thread: ThreadRecord) -> bool:
    return thread.status_type == "active" or bool(thread.active_turn_id)


def _select_recent_hand_off_threads(threads: list[ThreadRecord], *, limit: int) -> list[ThreadRecord]:
    active_threads = sorted((thread for thread in threads if _is_active_thread(thread)), key=_active_thread_sort_key, reverse=True)
    idle_threads = sorted((thread for thread in threads if not _is_active_thread(thread)), key=_active_thread_sort_key, reverse=True)
    return [*active_threads, *idle_threads][:limit]


async def _collect_messages(service: OrchestratorService, thread_id: str) -> list[tuple[str, str]]:
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


async def _delete_prior_telegram_topics(
    service: OrchestratorService,
    *,
    api: HttpTelegramBotApi,
    chat_id: int,
) -> None:
    for session in service.registry.list_telegram_sessions():
        if session.chat_id != chat_id or session.message_thread_id is None:
            continue
        try:
            await api.delete_forum_topic(chat_id, session.message_thread_id)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"Failed to delete prior Telegram topic {session.message_thread_id} for thread {session.thread_id}: {exc}"
            )
        finally:
            service.registry.delete_telegram_session(session.thread_id)


async def _run_thread_interaction(
    service: OrchestratorService,
    thread_id: str,
    *,
    telegram_bridge: TelegramOperatorBridge | None,
    max_events: int | None,
    no_history: bool,
    history_limit: int,
    refresh_seconds: float,
    allow_terminal_input: bool,
    ensure_loaded: bool = True,
) -> None:
    seen_message_ids: set[str] = set()
    approval_queue: asyncio.Queue[tuple[dict[str, Any], asyncio.Future[dict[str, Any]]]] = asyncio.Queue()
    active_approval: tuple[dict[str, Any], asyncio.Future[dict[str, Any]]] | None = None
    interactive_prompt = (
        PromptSession(PROMPT_TEXT)
        if allow_terminal_input
        and telegram_bridge is None
        and PromptSession is not None
        and patch_stdout is not None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        else None
    )

    # A thread may exist in the server listing but not be loaded on this connection yet.
    # Resume it before any history reads so thread/read works consistently in multi-thread hand-off mode.
    if ensure_loaded:
        await service.resume_thread(thread_id)

    async def sync_messages(*, print_history: bool) -> int:
        printed = 0
        messages = await _collect_messages(service, thread_id)
        already_seen = set(seen_message_ids)
        if print_history and history_limit > 0:
            for message_id, _ in messages:
                seen_message_ids.add(message_id)
            for message_id, text in messages[-history_limit:]:
                if message_id in already_seen:
                    continue
                await _emit_output(text, telegram_bridge)
                printed += 1
            return printed
        for message_id, text in messages:
            if message_id in seen_message_ids:
                continue
            await _emit_output(text, telegram_bridge)
            seen_message_ids.add(message_id)
            printed += 1
        return printed

    async def send_prompt_to_thread(prompt: str) -> tuple[str, str]:
        turn = await service.start_turn_on_thread(thread_id, prompt)
        return turn.turn_id, f"Started turn {turn.turn_id}"

    async def handle_server_request(method: str, params: dict[str, Any]) -> Any:
        if method != "item/commandExecution/requestApproval":
            return UNHANDLED
        if params.get("threadId") != thread_id:
            return UNHANDLED
        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        await approval_queue.put((params, response_future))
        return await response_future

    service.transport.add_request_handler(handle_server_request)

    if not no_history:
        await sync_messages(print_history=True)
    else:
        messages = await _collect_messages(service, thread_id)
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
            if telegram_bridge is not None:
                await telegram_bridge.send_text(
                    f"approval: {reason}",
                    buttons=_approval_button_labels(params) or ["approve", "cancel"],
                )
            else:
                await _emit_output(f"approval: {reason}", telegram_bridge)
            if command:
                await _emit_output(f"command: {command}", telegram_bridge)
            if telegram_bridge is None:
                await _emit_output(f"reply with: {_approval_help_text(params)}", telegram_bridge)
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
                await _emit_output(text, telegram_bridge)
                seen_message_ids.add(message_id)
            return
        await sync_messages(print_history=False)

    async def input_loop() -> None:
        while True:
            if telegram_bridge is not None:
                line = await telegram_bridge.read_input()
            elif interactive_prompt is not None:
                try:
                    line = await interactive_prompt.prompt_async()
                except EOFError:
                    return
            elif allow_terminal_input:
                line = await asyncio.to_thread(sys.stdin.readline)
                if line == "":
                    return
            else:
                await asyncio.sleep(3600)
                continue
            prompt = line.strip()
            if not prompt:
                continue
            if active_approval is not None:
                params, response_future = active_approval
                decision = _parse_approval_input(prompt, params)
                if decision is None:
                    await _emit_output(
                        f"invalid approval response; reply with: {_approval_help_text(params)}",
                        telegram_bridge,
                    )
                    continue
                response_future.set_result(decision)
                if telegram_bridge is not None:
                    await telegram_bridge.send_text(f"approval sent: {decision['decision']}", clear_buttons=True)
                else:
                    await _emit_output(f"approval sent: {decision['decision']}", telegram_bridge)
                continue
            try:
                input_busy.set()
                accepted_turn_id, action_label = await send_prompt_to_thread(prompt)
                user_message_id = _message_key(accepted_turn_id, "user", prompt)
                if user_message_id not in seen_message_ids:
                    seen_message_ids.add(user_message_id)
                    await _emit_output(f"user: {prompt}", telegram_bridge)
                await sync_messages(print_history=False)
                await _emit_output(action_label, telegram_bridge)
            except Exception as exc:  # noqa: BLE001
                await _emit_output(f"Send failed: {exc}", telegram_bridge)
                continue
            finally:
                input_busy.clear()

    refresh_task = asyncio.create_task(refresh_loop())
    approval_task = asyncio.create_task(approval_loop())
    prompt_context = patch_stdout() if interactive_prompt is not None and patch_stdout is not None else contextlib.nullcontext()
    with prompt_context:
        listener_task = asyncio.create_task(service.listen(thread_id, on_event, max_events=max_events))
        input_task = asyncio.create_task(input_loop())
        try:
            done, pending = await asyncio.wait({listener_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
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


@app.command(help="List known threads from the App Server and refresh the local registry cache.")
def threads(config: ConfigOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        rows = await service.list_threads()
        turns_by_thread = {row.thread_id: service.registry.list_turns(thread_id=row.thread_id) for row in rows}
        _render_threads(rows, turns_by_thread)

    _run(_execute_with_service(config, operation))


@app.command(help="Show locally cached metadata and recent turns for a thread.")
def inspect(thread_id: str, config: ConfigOption = None) -> None:
    service = _build_service(config)
    thread, turns = service.inspect_local(thread_id)
    if thread is None:
        raise typer.Exit(code=1)
    console.print(f"Thread: {thread.thread_id}")
    console.print(f"Name: {thread.name or ''}")
    console.print(f"Status: {thread.status_type}")
    console.print(f"Active turn: {thread.active_turn_id or ''}")
    _render_turns(turns[:10])


@app.command(help="Read a thread from the App Server and refresh its stored local snapshot.")
def read(thread_id: str, config: ConfigOption = None) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        thread = await service.read_thread(thread_id, include_turns=True)
        _render_threads([thread])
        _render_turns(service.registry.list_turns(thread_id=thread_id))

    _run(_execute_with_service(config, operation))


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

    _run(_execute_with_service(config, operation))


@app.command(name="listen-and-send", help="Listen to a thread and start a new turn for each line typed into the terminal.")
def listen_and_send(
    thread_id: str,
    interface: Annotated[
        OperatorInterface,
        typer.Option("--interface", help="Operator interface for output and follow-up input."),
    ] = OperatorInterface.CLI,
    telegram_chat_id: Annotated[
        int | None,
        typer.Option("--telegram-chat-id", help="Bind the Telegram interface to this chat ID immediately."),
    ] = None,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop after this many live events.")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Skip replaying recent thread messages before listening.")] = False,
    history_limit: Annotated[int, typer.Option("--history-limit", help="Number of most recent messages to replay before listening.")] = 20,
    refresh_seconds: Annotated[
        float,
        typer.Option("--refresh-seconds", help="Fallback snapshot refresh interval while listening."),
    ] = 2.0,
    config: ConfigOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        console.print(f"Listening and sending on thread {thread_id}")
        telegram_bridge: TelegramOperatorBridge | None = None
        try:
            if interface == OperatorInterface.TELEGRAM:
                if not service.config.telegram.enabled:
                    raise RuntimeError("Telegram interface requested but [telegram].bot_token is not configured.")
                service.registry.delete_telegram_session(thread_id)
                telegram_bridge = TelegramOperatorBridge(
                    thread_id=thread_id,
                    registry=service.registry,
                    config=service.config.telegram,
                    api=HttpTelegramBotApi(service.config.telegram),
                    chat_id=telegram_chat_id,
                )
                await telegram_bridge.start()
                await _emit_output("Telegram interface is active for this thread.", telegram_bridge)
            else:
                console.print("Type a line and press Enter to start a new turn. Send EOF to exit.")
            await _run_thread_interaction(
                service,
                thread_id,
                telegram_bridge=telegram_bridge,
                max_events=max_events,
                no_history=no_history,
                history_limit=history_limit,
                refresh_seconds=refresh_seconds,
                allow_terminal_input=interface == OperatorInterface.CLI,
            )
        finally:
            if telegram_bridge is not None:
                await telegram_bridge.close()

    _run(_execute_with_service(config, operation))


@app.command(name="hand-off", help="Attach recent Codex threads to Telegram as separate private threads.")
def hand_off(
    limit: Annotated[int, typer.Option("--limit", min=1, max=20, help="Maximum number of threads to hand off.")] = 5,
    telegram_chat_id: Annotated[
        int | None,
        typer.Option("--telegram-chat-id", help="Target Telegram chat ID. Defaults to [telegram].default_chat_id."),
    ] = None,
    max_events: Annotated[int | None, typer.Option("--max-events", help="Stop each live listener after this many events.")] = None,
    no_history: Annotated[bool, typer.Option("--no-history", help="Skip replaying recent thread messages before listening.")] = False,
    history_limit: Annotated[int, typer.Option("--history-limit", help="Number of most recent messages to replay before listening.")] = 20,
    refresh_seconds: Annotated[
        float,
        typer.Option("--refresh-seconds", help="Fallback snapshot refresh interval while listening."),
    ] = 2.0,
    config: ConfigOption = None,
) -> None:
    async def operation(service: OrchestratorService) -> None:
        await service.connect()
        if not service.config.telegram.enabled:
            raise RuntimeError("Telegram hand-off requires [telegram].bot_token to be configured.")

        threads = await service.list_threads(limit=max(limit * 10, 50))
        selected_threads = _select_recent_hand_off_threads(threads, limit=limit)
        if not selected_threads:
            console.print("No threads found to hand off.")
            return

        resumable_threads: list[ThreadRecord] = []
        for thread in selected_threads:
            try:
                await service.resume_thread(thread.thread_id)
            except Exception as exc:  # noqa: BLE001
                console.print(f"Skipping thread {thread.thread_id}: {exc}")
                continue
            resumable_threads.append(thread)

        if not resumable_threads:
            console.print("No resumable recent threads found to hand off.")
            return

        target_chat_id = telegram_chat_id if telegram_chat_id is not None else service.config.telegram.default_chat_id
        if target_chat_id is None:
            raise RuntimeError("Telegram hand-off requires [telegram].default_chat_id or --telegram-chat-id.")

        console.print(f"Handing off {len(resumable_threads)} thread(s) to Telegram chat {target_chat_id}")
        for thread in resumable_threads:
            console.print(f"- {build_topic_name(cwd=thread.cwd, thread_name=thread.name, thread_id=thread.thread_id)}")

        api = HttpTelegramBotApi(service.config.telegram)
        hub = TelegramBridgeHub(api=api, poll_timeout_seconds=service.config.telegram.poll_timeout_seconds)
        bridges: list[TelegramOperatorBridge] = []
        try:
            await _delete_prior_telegram_topics(service, api=api, chat_id=target_chat_id)
            for thread in resumable_threads:
                service.registry.delete_telegram_session(thread.thread_id)
                bridge = TelegramOperatorBridge(
                    thread_id=thread.thread_id,
                    registry=service.registry,
                    config=service.config.telegram,
                    api=api,
                    chat_id=target_chat_id,
                    topic_name=build_topic_name(cwd=thread.cwd, thread_name=thread.name, thread_id=thread.thread_id),
                    poll_updates=False,
                    owns_api=False,
                )
                await bridge.start()
                hub.add_bridge(bridge)
                bridges.append(bridge)
            await hub.start()
            await asyncio.gather(
                *[
                    _run_thread_interaction(
                        service,
                        thread.thread_id,
                        telegram_bridge=bridge,
                        max_events=max_events,
                        no_history=no_history,
                        history_limit=history_limit,
                        refresh_seconds=refresh_seconds,
                        allow_terminal_input=False,
                        ensure_loaded=False,
                    )
                    for thread, bridge in zip(resumable_threads, bridges, strict=True)
                ]
            )
        finally:
            for bridge in bridges:
                await bridge.close()
            await hub.close()

    _run(_execute_with_service(config, operation))


@app.command(help="Validate local configuration and test connectivity to the Codex App Server.")
def doctor(config: ConfigOption = None) -> None:
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

    _run(_execute_with_service(config, operation))
