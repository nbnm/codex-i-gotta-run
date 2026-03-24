# codex-i-gotta-run

Local-only prototype sidecar for orchestrating Codex threads through the Codex App Server.

The project treats the App Server as the source of truth for live thread and turn state. Local JSON files are used only for cached projections, queued inputs, and restart recovery.

## What It Does

- connects to a local Codex App Server over `stdio`
- initializes a client session with JSON-RPC
- lists, reads, and resumes threads
- shows each thread's core folder and latest known turn timestamp
- starts new turns and steers active turns
- interrupts active turns
- persists local thread, turn, queue, and event state
- tails live events for a thread after resume
- listens to live thread events and prints them to the console

## Requirements

- Python 3.12+
- `uv`
- access to a local Codex App Server command

## Install

```bash
uv sync
```

Run the CLI with:

```bash
uv run codex-i-gotta-run --help
```

## Configuration

Configuration precedence is:

1. CLI flags
2. environment variables
3. local TOML config
4. code defaults

Supported environment variables:

- `CODEX_I_GOTTA_RUN_SERVER_CMD`
- `CODEX_I_GOTTA_RUN_SERVER_CWD`
- `CODEX_I_GOTTA_RUN_DATA_DIR`
- `CODEX_I_GOTTA_RUN_LOG_LEVEL`
- `CODEX_I_GOTTA_RUN_EXPERIMENTAL_API`
- `CODEX_I_GOTTA_RUN_OPTOUT_NOTIFICATIONS`

Example config file:

```toml
[server]
command = ["codex", "app-server"]
cwd = "/absolute/path/to/workspace"
experimental_api = false
opt_out_notification_methods = []

[registry]
data_dir = "/absolute/path/to/local/registry"

[logging]
level = "INFO"

[client]
name = "codex-i-gotta-run"
title = "codex-i-gotta-run"
version = "0.1.0"
```

Example with environment variables:

```bash
export CODEX_I_GOTTA_RUN_SERVER_CMD="codex app-server"
export CODEX_I_GOTTA_RUN_DATA_DIR="$PWD/.local-data"
uv run codex-i-gotta-run doctor
```

Example with a config file:

```bash
uv run codex-i-gotta-run doctor --config ./config.toml
```

## CLI Commands

Core commands:

- `connect`
- `threads`
- `inspect <thread_id>`
- `read <thread_id>`
- `resume <thread_id>`
- `start "<prompt>"`
- `continue <thread_id> "<prompt>"`
- `steer <thread_id> <turn_id> "<prompt>"`
- `interrupt <thread_id> <turn_id>`
- `tail <thread_id> [--max-events N]`
- `listen <thread_id> [--max-events N]`
- `queue <thread_id> "<prompt>" [--mode auto|steer|continue]`
- `autosteer <thread_id>`
- `status`
- `doctor`

Examples:

```bash
uv run codex-i-gotta-run doctor
uv run codex-i-gotta-run connect
uv run codex-i-gotta-run threads
uv run codex-i-gotta-run start "Summarize the current repo state"
uv run codex-i-gotta-run read <thread_id>
uv run codex-i-gotta-run continue <thread_id> "Add tests for the latest change"
uv run codex-i-gotta-run steer <thread_id> <turn_id> "Stop and rewrite this as a smaller patch"
uv run codex-i-gotta-run interrupt <thread_id> <turn_id>
uv run codex-i-gotta-run tail <thread_id> --max-events 10
uv run codex-i-gotta-run listen <thread_id> --max-events 10
uv run codex-i-gotta-run listen <thread_id> --no-history
uv run codex-i-gotta-run queue <thread_id> "Finish once the current task completes"
uv run codex-i-gotta-run autosteer <thread_id>
uv run codex-i-gotta-run status
```

Notes:

- `inspect` and `status` read from the local registry and do not require a live App Server connection.
- `threads` includes the thread `cwd` as `Core Folder` and the latest known turn timestamp as `Last Turn`.
- `tail` resumes the thread first so the App Server will emit live notifications.
- `listen` replays recent known events, then resumes the thread and writes live event lines directly to the terminal.
- use `--no-history` if you only want newly arriving events.
- `autosteer` uses the v0 rule set:
  - active turn with known `turnId` -> `turn/steer`
  - no active turn -> `turn/start`
  - uncertain state -> refresh before acting

## Local State

By default, the registry lives under the platform-local application data directory:

- macOS: `~/Library/Application Support/codex-i-gotta-run`
- Linux: `~/.local/share/codex-i-gotta-run`
- Windows: `%APPDATA%/codex-i-gotta-run`

The registry stores:

- thread snapshots
- turn snapshots
- queued input records
- connection state
- append-only `events.jsonl`

This state is a cache and recovery aid, not the primary protocol surface.

## Development

Run tests:

```bash
uv run pytest -q
```

Run a compile pass:

```bash
python3 -m compileall src tests
```

## Demo Without A Real App Server

The test suite includes a fake stdio App Server at [tests/fake_app_server.py](/Volumes/Work/Projects/codex-i-gotta-run/tests/fake_app_server.py). You can use it for local CLI smoke tests:

```bash
export CODEX_I_GOTTA_RUN_SERVER_CMD="python -m tests.fake_app_server"
export FAKE_APP_SERVER_STATE_PATH="$PWD/.fake-app-server-state.json"
export CODEX_I_GOTTA_RUN_DATA_DIR="$PWD/.fake-registry"

uv run codex-i-gotta-run start "hold the turn"
uv run codex-i-gotta-run threads
```

## Project Docs

- [AGENTS.md](/Volumes/Work/Projects/codex-i-gotta-run/AGENTS.md)
- [docs/tech-stack-plan.md](/Volumes/Work/Projects/codex-i-gotta-run/docs/tech-stack-plan.md)
- [docs/assumptions.md](/Volumes/Work/Projects/codex-i-gotta-run/docs/assumptions.md)
