# AGENTS.md

## Project mission

Build a local-only prototype named `codex-thread-orchestrator`.

This prototype uses the Codex App Server as the primary control plane.

The prototype must:
- connect to a local Codex App Server
- discover and track Codex threads through App Server APIs
- observe live thread and turn events
- resume existing threads
- start new turns on known threads
- steer active in-flight turns
- persist enough local state to recover after restarts
- exclude Telegram integration for v0

This repository is for a prototype, not a production service.

## Product definition

Use these terms consistently.

- **thread** = a persisted Codex conversation/workstream
- **turn** = one user instruction plus the agent work that follows
- **active turn** = a turn currently in progress for a thread
- **steering** = adding more user input to an active in-flight turn
- **continue** = starting the next turn on an existing thread after the prior turn is terminal
- **registry** = local persisted state for known threads, turns, statuses, queued inputs, and recent events
- **sidecar** = the local process in this repo that connects to Codex App Server and exposes CLI-driven orchestration behavior
- **auto steering** = policy-based decision logic that either steers an active turn or starts a follow-up turn when appropriate

## Primary architecture

This project uses **Option 1**.

That means:
- Codex App Server is the source of truth for live thread and turn state
- local persisted state is only a cache and recovery aid
- filesystem session files are not the primary protocol
- file polling may be used for debugging or recovery only, if ever added later

Do not design around `.codex/sessions` as the main API.

## Hard constraints

- Do not edit, delete, or rewrite files under `~/.codex/`.
- Do not rely on filesystem polling as the main event source.
- Do not use screen scraping or UI automation.
- Do not infer active state from file mtimes.
- Do not guess thread state when the App Server can answer directly.
- Prefer explicit `threadId` and `turnId` values over heuristics.
- Keep the prototype single-user and local-first.
- Skip Telegram entirely in v0.
- Skip cloud deployment in v0.
- Skip browser UI in v0 unless specifically requested later.
- Favor deterministic behavior over "smart" autonomy.

## Official-docs rule

When Codex behavior is unclear:
1. prefer official OpenAI Codex App Server docs
2. prefer official OpenAI Codex AGENTS.md docs
3. record assumptions in `docs/assumptions.md`

If an API shape is uncertain, do not invent behavior silently. Document the uncertainty.

## v0 goals

The prototype is successful if it can:

1. connect to a local Codex App Server
2. initialize a session correctly
3. list known threads
4. read thread metadata without resuming when needed
5. resume a known thread
6. start a turn on a known thread
7. receive and persist live events
8. detect whether a thread has an active turn
9. steer that active turn with additional input
10. interrupt a turn
11. recover local state after restart

## Out of scope for v0

Do not build these yet:
- Telegram integration
- Slack integration
- webhooks
- browser frontend
- multi-user auth
- remote deployment
- file-based session scraping as core logic
- direct writes to Codex internal storage
- generalized workflow engine
- review UI
- approvals UI beyond minimal CLI support
- cross-machine sync

Document future ideas, but do not implement them.

## Core design principles

- Treat App Server as the live truth.
- Treat local registry state as cached, replayable, and replaceable.
- Prefer explicit thread lifecycle methods over inferred state.
- Prefer event-driven updates over polling.
- Use thread IDs and turn IDs as canonical identifiers.
- Maintain one active-turn record per thread.
- Make steering behavior explainable and auditable.
- Keep all commands restart-safe and idempotent where possible.

## Repository conventions

- Language: Python 3.12+.
- Project management: `uv` with `pyproject.toml`.
- CLI framework: Typer.
- Output formatting: Rich for human-readable local operator output.
- Validation and typed boundaries: Pydantic v2.
- Tests: `pytest`.
- Logging: structured stdlib `logging`.
- Runtime model: one local async `asyncio` process for transport, ingestion, queueing, and command execution.
- Configuration precedence: CLI flags, then env vars, then local TOML config, then code defaults.
- Favor explicit typed domain models and conservative protocol adapters over dynamic or implicit behavior.

Suggested repo layout:
- `src/codex_thread_orchestrator/` for application code
- `tests/` for unit and CLI tests
- `docs/` for assumptions and design notes

## Primary modules

### 1. App Server transport client

Responsibilities:
- connect to Codex App Server over `stdio` first
- spawn a configured local App Server command
- send JSON-RPC requests
- receive JSON-RPC responses and notifications
- perform initialization handshake
- manage reconnect behavior conservatively
- expose a typed internal event emitter or async event stream
- support graceful shutdown

Implementation notes:
- start with `stdio`
- newline-delimited JSON in and out unless official docs require otherwise
- maintain request IDs
- correlate responses with pending requests
- support notification handlers by method name
- never rely on filesystem polling as the transport

### 2. Thread registry

Responsibilities:
- persist known threads
- persist active turn per thread
- persist latest status and summary
- persist enough event history for debugging and replay
- persist queued follow-up inputs
- support crash-safe restarts

Default v0 storage:
- JSON files, not SQLite
- append-only raw event log
- projected thread snapshots
- projected turn snapshots
- queued input records
- connection-state snapshot

Logical collections to preserve even in file-backed storage:

#### threads
- `thread_id` unique
- `name` nullable
- `cwd` nullable
- `source_kind` nullable
- `created_at` nullable
- `updated_at` nullable
- `status_type`
- `active_turn_id` nullable
- `last_seen_at`
- `archived` boolean default false

#### turns
- `turn_id` unique
- `thread_id`
- `status`
- `started_at` nullable
- `completed_at` nullable
- `summary` nullable
- `error_json` nullable

#### events
- `thread_id` nullable
- `turn_id` nullable
- `event_type`
- `payload_json`
- `received_at`

#### queued_inputs
- `thread_id`
- `mode` enum(`steer`, `continue`, `auto`)
- `text`
- `status` enum(`queued`, `submitted`, `done`, `failed`, `cancelled`)
- `created_at`
- `submitted_at` nullable
- `completed_at` nullable
- `error` nullable

#### connection_state
- `app_server_instance` nullable
- `initialized_at` nullable
- `last_event_at` nullable
- `last_error` nullable

These are logical record types and projections. They do not require a relational database in v0.

### 3. App Server adapter

Responsibilities:
- wrap protocol methods behind stable internal functions
- normalize responses into domain models
- shield the rest of the app from raw transport concerns

Required operations:
- `initialize_client()`
- `list_threads(filters=None)`
- `read_thread(thread_id, include_turns=False)`
- `resume_thread(thread_id, options=None)`
- `start_thread(options=None)`
- `start_turn(thread_id, input_text, options=None)`
- `steer_turn(thread_id, turn_id, input_text)`
- `interrupt_turn(thread_id, turn_id)`
- `unsubscribe_thread(thread_id)` if needed later

The adapter should also surface typed notifications for:
- thread started
- thread status changed
- thread archived, unarchived, or closed
- turn started
- turn completed
- turn diff updated
- turn plan updated
- item started and item completed
- agent message delta
- approvals or user input requests when encountered

If an API shape is uncertain, document the assumption in `docs/assumptions.md` rather than silently freezing guessed behavior into the adapter.

### 4. Event ingestion pipeline

Responsibilities:
- validate incoming notifications
- write raw event payloads to the registry
- update thread and turn projections
- emit high-level internal events for CLI output and tests

Rules:
- raw event first, projection second
- projection updates must be idempotent
- duplicate notifications must not corrupt state
- unknown event types must be logged and persisted, not discarded silently

### 5. Steering engine

Responsibilities:
- decide whether new input should become:
  - `turn/steer` on the active turn
  - or `turn/start` as the next turn
- enforce one active turn per thread
- support manual and queued input modes
- provide deterministic behavior

v0 decision rule:
- if a thread has an active in-progress turn with known `turnId`, steering uses `turn/steer`
- if a thread has no active turn, continuation uses `turn/start`
- if local state is stale or uncertain, refresh via `read_thread()` or `resume_thread()` before acting
- never guess the active turn ID

### 6. CLI

Provide a minimal local CLI.

Required commands:
- `connect` - connect to App Server and initialize
- `threads` - list known threads
- `inspect <threadId>` - show known metadata and latest turn state
- `read <threadId>` - fetch stored thread data from App Server
- `resume <threadId>` - resume a thread into the active session
- `start "<prompt>"` - create a new thread and start its first turn
- `continue <threadId> "<prompt>"` - start a new turn on an existing thread
- `steer <threadId> <turnId> "<prompt>"` - steer an active turn
- `interrupt <threadId> <turnId>` - interrupt an active turn
- `tail <threadId>` - stream formatted live events for a thread
- `queue <threadId> "<prompt>"` - enqueue follow-up input
- `autosteer <threadId>` - process queued inputs for a thread using v0 rules
- `status` - show connection state, active turns, and queue state
- `doctor` - validate environment and connectivity

Optional later:
- `archive <threadId>`
- `fork <threadId>`
- `replay-events <threadId>`
- `gc-events`
- `sync`

## Steering semantics

In this repository, "steering" has a precise meaning.

### Steering

Use steering only when:
- there is an active in-flight turn
- the active `turnId` is known
- the goal is to append more user input to the current turn

### Continuation

Use continuation when:
- the previous turn is terminal
- the user wants the next instruction executed in the same thread

### Interruption

Use interruption when:
- the current active turn should stop
- the agent is headed in the wrong direction
- the user wants to replace the current approach

Do not conflate these operations.

## Auto steering in v0

For this project, auto steering means controlled automatic routing of queued inputs using current thread state.

Rules:
- queued inputs must be persisted before submission
- the engine must check current thread state before acting
- if there is a known active turn, queued input may become `steer`
- if there is no active turn, queued input may become a new turn
- if state is stale or uncertain, refresh first
- if thread state remains uncertain after refresh, fail safely and leave an auditable error

## Testing requirements

At minimum, cover:
- initialization and connection handshake
- JSON-RPC request and response correlation
- notification handling during in-flight requests
- adapter normalization of thread and turn data
- registry recovery after restart
- duplicate event replay idempotency
- steering versus continuation routing
- interruption behavior
- CLI command behavior for local operator workflows

Prefer an in-process fake JSON-RPC App Server for tests over real-server-only testing.

## Change discipline

- Keep changes restart-safe and local-only.
- Prefer explicit domain models, explicit IDs, and auditable state transitions.
- Do not add production-only infrastructure unless the repo explicitly graduates beyond prototype scope.
- Do not implement out-of-scope integrations under feature flags "just in case".
- When uncertain about Codex behavior, document assumptions first and code second.
