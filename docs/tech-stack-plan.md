# Tech Stack Plan For `codex-i-gotta-run`

## Summary

Use a local-only Python stack optimized for deterministic behavior, explicit state transitions, and easy debugging.

- Language and runtime: Python 3.12+
- Project tooling: `uv` with `pyproject.toml`
- Process model: single async `asyncio` process
- CLI: Typer + Rich
- Validation and models: Pydantic v2
- Persistence: JSON files with projected snapshots and an append-only event log
- Logging: structured stdlib `logging`
- Config: local TOML config file with `--config` selection and code defaults
- Tests: `pytest` with an in-process fake JSON-RPC App Server over `stdio`

## Key Stack Decisions

### Transport layer

- Implement JSON-RPC over `stdio` first.
- The sidecar spawns a configured local Codex App Server command and owns lifecycle, reconnect, and graceful shutdown.
- The stack does not target remote App Server endpoints.
- Use `asyncio.subprocess` for process I/O.
- Use `asyncio` tasks for request correlation, notification handling, and shutdown coordination.

### Internal architecture

- Separate modules for `transport`, `adapter`, `registry`, `ingestion`, and `cli`.
- Keep raw protocol payloads at the transport boundary.
- Normalize responses into typed domain models in the adapter.
- Use explicit thread IDs and turn IDs throughout.
- Never derive active state from heuristics when a refresh can answer it.

### Registry and storage

- Store local state in a repo-external app data directory, not under `~/.codex/`.
- Use JSON files for:
  - thread snapshots
  - turn snapshots
  - connection-state snapshot
  - append-only event log
- Preserve raw events before applying projections.
- Projection updates must be idempotent.

### Operator UX

- Typer provides the CLI command structure.
- Rich is used for `threads`, `inspect`, `read`, `doctor`, and `listen` output.
- Human-readable local output is the default in v0.
- `listen` should prioritize recent actual thread message history before printing newly detected messages, suppress older backlog replay after the initial history window has been shown, and use periodic snapshot refresh as a fallback when live message events are too sparse.
- `listen-and-send` should keep a stable bottom prompt in interactive terminals while new output is rendered above it.
- stderr noise from the App Server should stay out of normal operator output unless debug logging is enabled.
- JSON output can be added later for scripting, but it is not the default.

### Configuration

- Runtime settings come from a TOML config file, optionally selected with `--config`.
- Environment variables are not used as a runtime configuration layer.
- Config must cover at least:
  - App Server spawn command and args
  - local data directory
  - log level and output mode
  - reconnect policy knobs

### Testing

- Use `pytest` and temp directories for registry recovery tests.
- Use an in-process fake stdio JSON-RPC server to simulate initialize, list, read, resume, turn start, command approval requests, and notification flows.
- Keep real-server integration tests optional and separate from the default test suite.

## Public Interfaces And Internal Contracts

- CLI surface should match the planned commands:
  - `threads`
  - `inspect`
  - `read`
  - `listen`
  - `listen-and-send`
  - `doctor`
- Internal adapter contract should expose stable Python methods matching the product brief:
  - `initialize_client()`
  - `list_threads(...)`
  - `read_thread(...)`
  - `resume_thread(...)`
  - `start_thread(...)`
  - `start_turn(...)`
- Core model types should be explicit Pydantic models for:
  - `ThreadRecord`
  - `TurnRecord`
  - `EventRecord`
  - `ConnectionState`
  - request and response envelopes
  - known notification payloads
- Unknown notification types must still deserialize into a generic event wrapper and be persisted and logged.

## Test Plan

- Transport tests:
  - request ID correlation
  - partial stdout line handling
  - notification delivery during in-flight requests
  - reconnect and shutdown behavior
- Adapter tests:
  - normalization of known thread and turn payloads
  - handling of uncertain or partial API fields
  - explicit failure behavior on malformed responses
- Registry and ingestion tests:
  - raw event written before projection update
  - duplicate notification replay is idempotent
  - restart recovery rebuilds live state from snapshots and event log
  - unknown events are retained and surfaced
- Turn-start tests:
  - a new typed line in `listen-and-send` always starts a fresh turn on the selected thread
  - configured `turn_start_options` are forwarded into those new turns
  - command approval requests can be accepted from the terminal and allow the turn to continue
- CLI tests:
  - command parsing
  - human-readable threads, inspect, read, doctor, listen, and listen-and-send output
  - approval prompts are shown in the console with accepted reply labels
  - operator error messages for missing config, missing thread, or uncertain state

## Assumptions And Defaults

- The current scope remains single-user and local-only.
- `asyncio` is sufficient; no daemon and worker split is planned in the current scope.
- JSON files are acceptable because the App Server is the source of truth, not the local registry.
- The first transport is `stdio`; WebSocket remains a later extension.
- Any unclear Codex App Server behavior must be documented in `docs/assumptions.md` rather than silently codified.
