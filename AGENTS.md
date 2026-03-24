# AGENTS.md

## Project mission

Build a local-only tool named `codex-i-gotta-run`.

This tool uses a local Codex App Server as the primary control plane.

The tool must:
- connect only to a local Codex App Server
- discover and track Codex threads through App Server APIs
- observe live thread and turn events
- resume existing threads
- start new turns on known threads
- steer active in-flight turns
- persist enough local state to recover after restarts

This repository is for a single-user local workflow, not a cloud service.

## Product definition

Use these terms consistently.

- **thread** = a persisted Codex conversation/workstream
- **turn** = one user instruction plus the agent work that follows
- **active turn** = a turn currently in progress for a thread
- **steering** = adding more user input to an active in-flight turn
- **continue** = starting the next turn on an existing thread after the prior turn is terminal
- **registry** = local persisted state for known threads, turns, statuses, and recent events
- **sidecar** = the local process in this repo that connects to Codex App Server and exposes CLI-driven orchestration behavior
- **auto steering** = deferred future logic for policy-based routing of follow-up inputs, not part of the current CLI

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
- Keep the tool single-user and local-first.
- Skip cloud deployment in v0.
- Skip browser UI in v0 unless specifically requested later.
- Favor deterministic behavior over "smart" autonomy.

## Official-docs rule

When Codex behavior is unclear:
1. prefer official OpenAI Codex App Server docs
2. prefer official OpenAI Codex AGENTS.md docs
3. record assumptions in `docs/assumptions.md`

If an API shape is uncertain, do not invent behavior silently. Document the uncertainty.

## Current goals

The current local-only tool is successful if it can:

1. connect to a local Codex App Server
2. initialize a session correctly
3. list known threads
4. read thread metadata without resuming when needed
5. resume a known thread
6. start a turn on a known thread
7. receive and persist live events
8. recover local state after restart

## Out of scope for v0

Do not build these yet:
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
- Make terminal send and approval behavior explainable and auditable.
- Keep all commands restart-safe and idempotent where possible.

## Repository conventions

- Language: Python 3.12+.
- Project management: `uv` with `pyproject.toml`.
- CLI framework: Typer.
- Output formatting: Rich for human-readable local operator output.
- Validation and typed boundaries: Pydantic v2.
- Tests: `pytest`.
- Logging: structured stdlib `logging`.
- Runtime model: one local async `asyncio` process for transport, ingestion, and command execution.
- Configuration source: local TOML config file selected by CLI, then code defaults.
- Environment variables are not part of runtime configuration.
- Favor explicit typed domain models and conservative protocol adapters over dynamic or implicit behavior.

Suggested repo layout:
- `src/` for application code
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
- support crash-safe restarts

Default v0 storage:
- JSON files, not SQLite
- append-only raw event log
- projected thread snapshots
- projected turn snapshots
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
- route terminal-originated follow-up input into explicit `turn/start` calls
- keep terminal-driven behavior deterministic and restart-safe
- avoid guessing or mutating an in-flight turn from the terminal path

v0 decision rule:
- terminal input always starts a fresh next turn via `turn/start`
- forward explicit turn-start options from config when present
- use `read_thread()` and periodic refreshes for message visibility rather than active-turn mutation

### 6. CLI

Provide a minimal local CLI focused on thread discovery, inspection, and live listening.

Required commands:
- `threads` - list known threads
- `threads` output should include each thread's core folder (`cwd`) and latest known turn timestamp when available
- `inspect <threadId>` - show known metadata and latest turn state
- `read <threadId>` - fetch stored thread data from App Server
- `listen <threadId>` - print recent thread messages first, then newly detected messages to the local console; support skipping history and limiting replay depth when requested, without replaying the older backlog again after resume, and use periodic refresh as a fallback when live message events are not emitted
- `listen-and-send <threadId>` - run the same live listening flow as `listen`, while also accepting terminal input and sending each typed line as a fresh next turn on that thread; do not reuse an in-flight turn from the terminal path, and surface command-approval requests in the console when they occur
- `listen-and-send` should keep a stable bottom input line in interactive terminals while new output prints above it
- `doctor` - validate config file, local server command, and connectivity

Optional later:
- `connect`
- `tail <threadId>`
- `resume <threadId>`
- `start "<prompt>"`
- `status`
- `archive <threadId>`
- `fork <threadId>`
- `replay-events <threadId>`
- `gc-events`
- `sync`

## Terminal Send Semantics

In the current CLI, terminal follow-up input has a precise meaning.

### Terminal Send

Use terminal send when:
- the operator types a new line into `listen-and-send`
- the goal is to start the next explicit turn on the same thread
- the operator wants configured `turn_start_options` to apply

Approval handling:
- if the App Server requests approval for a command execution, the CLI must print the approval reason and command
- the operator must be able to reply with an explicit decision such as `approve` or `cancel`
- the turn should remain paused until the approval response is sent

Rules:
- typed terminal input should always create the next explicit turn on the same thread
- configured turn-start options should be forwarded when starting that turn
- if the App Server pauses for command approval, the CLI must surface the prompt and wait for a reply
- if state is stale or uncertain for message display, refresh via `read_thread()` rather than guessing

## Testing requirements

At minimum, cover:
- initialization and connection handshake
- JSON-RPC request and response correlation
- notification handling during in-flight requests
- adapter normalization of thread and turn data
- registry recovery after restart
- duplicate event replay idempotency
- turn-start routing for terminal input
- approval handling for command execution requests
- CLI command behavior for local operator workflows

Prefer an in-process fake JSON-RPC App Server for tests over real-server-only testing.

## Change discipline

- Keep changes restart-safe and local-only.
- Prefer explicit domain models, explicit IDs, and auditable state transitions.
- Do not add cloud or multi-user infrastructure unless the repo explicitly expands beyond the current local-only scope.
- Do not implement out-of-scope integrations under feature flags "just in case".
- When uncertain about Codex behavior, document assumptions first and code second.
