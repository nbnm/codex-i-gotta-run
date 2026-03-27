# codex-i-gotta-run

Local-only prototype sidecar for orchestrating Codex threads through the Codex App Server.

The project treats the App Server as the source of truth for live thread and turn state. Local JSON files are used only for cached projections, recent events, and restart recovery.

## What It Does

- connects to a local Codex App Server over `stdio`
- initializes a client session with JSON-RPC
- lists and reads threads, and resumes them internally when needed for listening
- shows each thread's core folder and latest known turn timestamp
- persists local thread, turn, and event state
- validates local connectivity with `doctor`
- replays recent actual thread messages and then listens for new messages in the console
- lets an operator start a fresh next turn from the terminal while listening
- can route `listen-and-send` operator I/O through Telegram using a private chat, reusing `message_thread_id` when Telegram provides threaded private-chat updates
- can hand off the 5 most recently active Codex threads to Telegram, with one Telegram private thread/topic per Codex thread

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

Runtime settings live in a TOML config file.

- pass a config file explicitly with `--config /path/to/config.toml`
- if `--config` is omitted, the CLI will use `./config.toml` when that file exists
- code defaults still apply for omitted fields such as the default local registry path

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

[telegram]
# Resolve the bot token from this environment variable name.
telegram_bot_token_env = "TELEGRAM_BOT_TOKEN"
api_base_url = "https://api.telegram.org"
poll_timeout_seconds = 30
# Private threaded chat used for Telegram operator mode.
# `listen-and-send` reuses `message_thread_id` when Telegram provides it.
# `hand-off` creates one private thread/topic per selected Codex thread in this chat.
telegram_default_chat_id_env = "TELEGRAM_DEFAULT_CHAT_ID"
# Resolve allowed Telegram chat IDs from a comma-separated environment variable.
telegram_allowed_chat_ids_env = "TELEGRAM_ALLOWED_CHAT_IDS"
# Resolve the allowed username and attention-mention target from this environment variable name.
telegram_bot_allow_username = "TELEGRAM_BOT_ALLOW_USERNAME"

[turn_start_options]
# Raw passthrough options for new turns started on existing threads.
# Use this when `listen-and-send` needs to start the next turn and you want
# the same configured sandbox/approval defaults applied every time.
# sandbox_mode = "danger-full-access"
# approval_policy = "never"
```

Example with a config file:

```bash
uv run codex-i-gotta-run doctor --config ./config.toml
```

Telegram values are env-name references, not inline secrets or inline chat identifiers. Export the real values before running the CLI:

```bash
export TELEGRAM_BOT_TOKEN=123456:abc...
export TELEGRAM_BOT_ALLOW_USERNAME=@your_username
export TELEGRAM_DEFAULT_CHAT_ID=208920039
export TELEGRAM_ALLOWED_CHAT_IDS=208920039,-1001234567890
```

Notes:

- `telegram_bot_token_env` points to the bot token environment variable.
- `telegram_bot_allow_username` points to the single allowed Telegram username and is also used for approval and final-answer mentions.
- `telegram_default_chat_id_env` is still required for proactive Telegram flows such as `hand-off`, because the bot must know which chat to target before an inbound message binds a session.
- `telegram_allowed_chat_ids_env` expects a comma-separated list of numeric chat IDs.

## CLI Commands

Core commands:

- `threads`
- `inspect <thread_id>`
- `read <thread_id>`
- `listen <thread_id> [--max-events N] [--no-history] [--history-limit N] [--refresh-seconds N]`
- `listen-and-send <thread_id> [--max-events N] [--no-history] [--history-limit N] [--refresh-seconds N]`
- `listen-and-send <thread_id> --interface telegram [--telegram-chat-id CHAT_ID]`
- `hand-off [--limit N] [--telegram-chat-id CHAT_ID] [--max-events N]`
- `doctor`

Examples:

```bash
uv run codex-i-gotta-run doctor
uv run codex-i-gotta-run threads
uv run codex-i-gotta-run inspect <thread_id>
uv run codex-i-gotta-run read <thread_id>
uv run codex-i-gotta-run listen <thread_id> --max-events 10
uv run codex-i-gotta-run listen <thread_id> --no-history
uv run codex-i-gotta-run listen <thread_id> --history-limit 10
uv run codex-i-gotta-run listen <thread_id> --refresh-seconds 1
uv run codex-i-gotta-run listen-and-send <thread_id>
uv run codex-i-gotta-run listen-and-send <thread_id> --interface telegram
uv run codex-i-gotta-run hand-off
```

Notes:

- `inspect` reads from the local registry and does not require a live App Server connection.
- `threads` includes the thread `cwd` as `Core Folder` and the latest known turn timestamp as `Last Turn`.
- `read` refreshes the locally cached thread snapshot from the App Server.
- `listen` replays recent actual thread messages from history, then resumes the thread and prints newly detected messages.
- `listen` marks the full current snapshot as already seen before live listening starts, so older backlog messages do not get replayed again on the first resume-triggered refresh.
- `listen` prefers real live message events when they arrive, but it also performs periodic `thread/read` refreshes while attached so new messages still appear when the App Server only emits status-style notifications on that connection.
- `listen` ignores token-by-token command or message delta fragments in console output and waits for stable snapshot-backed messages such as `assistant/commentary: ...`.
- `listen-and-send` uses the same live listening flow as `listen`, but it also reads stdin and sends each non-empty typed line as a fresh `turn/start` on the same thread.
- `listen-and-send --interface telegram` uses the same turn semantics, but sends output and accepts follow-up input through Telegram instead of stdin/stdout.
- Telegram mode uses a private chat with the bot. When Telegram includes `message_thread_id` for that chat, the sidecar reuses it so the run stays scoped to that private thread.
- `hand-off` prefers the most recently active Codex threads from the App Server and backfills with the most recent idle threads until `--limit` is reached, with a default of `5`, then creates one Telegram private thread/topic per selected Codex thread and runs the same listen/send flow for each of them concurrently.
- `hand-off` names each Telegram topic as `<last cwd folder> | <thread name>`, falling back to the short thread ID when a thread name is missing.
- `hand-off` uses one shared Telegram Bot API polling loop for all attached threads so updates are consumed once and dispatched to the matching Codex thread binding.
- before `hand-off` creates a new set of Telegram topics in the target chat, it deletes previously tracked Telegram topics for that same chat and clears their local session bindings.
- `listen-and-send` does not reuse an active in-flight turn. This keeps terminal input on the explicit `turn_start_options` path from `config.toml`.
- when `listen-and-send` starts a new turn on a thread, it forwards configured `turn_start_options` from `config.toml` into `turn/start`. This is the deterministic way to keep new terminal-started turns on the configured approval and sandbox defaults.
- when `listen-and-send` is attached to an interactive TTY, it keeps a stable bottom input prompt and prints new thread output above it.
- if the App Server requests command approval during `listen-and-send`, the CLI prints the approval reason and command, then waits for a terminal reply such as `approve` or `cancel`.
- in Telegram private-thread mode, the run starts in the configured private chat and adopts the first seen `message_thread_id` for that run when Telegram includes one.
- once a Telegram `message_thread_id` has been observed for the run, only messages from that same private thread are accepted as operator input.
- in Telegram mode, role prefixes such as `user`, `approval`, and `assistant/commentary` are rendered in bold, approval prompts use Telegram reply-keyboard buttons such as `approve`, `approve-amend`, and `cancel`, and approval/final-answer messages explicitly mention the configured username.
- `listen-and-send` exits cleanly on EOF, so `Ctrl-D` ends the session.
- use `--no-history` if you only want future messages after the listener attaches.
- use `--history-limit` to control how many recent messages are replayed first. The default is `20`, and older backlog messages remain suppressed unless they are part of the selected replay window.
- use `--refresh-seconds` to control the fallback snapshot refresh interval while listening. The default is `2.0`.
- `listen` is meant for operator visibility, so it prefers showing recent message history first instead of waiting silently for the next event.
- App Server stderr warnings are suppressed at normal log levels so the console stays focused on thread messages.

## Local State

By default, the registry lives under the platform-local application data directory:

- macOS: `~/Library/Application Support/codex-i-gotta-run`
- Linux: `~/.local/share/codex-i-gotta-run`
- Windows: `%APPDATA%/codex-i-gotta-run`

The registry stores:

- thread snapshots
- turn snapshots
- connection state
- append-only `events.jsonl`
- Telegram session snapshots, including the bound chat, optional private-chat `message_thread_id`, tracked Telegram topic bindings, and update offset for the current run

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
export FAKE_APP_SERVER_STATE_PATH="$PWD/.fake-app-server-state.json"
cat > ./fake-config.toml <<'EOF'
[server]
command = ["python3", "-m", "tests.fake_app_server"]

[registry]
data_dir = "./.fake-registry"
EOF

uv run codex-i-gotta-run doctor --config ./fake-config.toml
uv run codex-i-gotta-run threads --config ./fake-config.toml
```

## Project Docs

- [AGENTS.md](/Volumes/Work/Projects/codex-i-gotta-run/AGENTS.md)
- [docs/tech-stack-plan.md](/Volumes/Work/Projects/codex-i-gotta-run/docs/tech-stack-plan.md)
- [docs/assumptions.md](/Volumes/Work/Projects/codex-i-gotta-run/docs/assumptions.md)
