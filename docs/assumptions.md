# Assumptions

This project implements the Codex App Server integration against the currently documented stable JSON-RPC flow:

- one `initialize` request followed by an `initialized` notification per connection
- `thread/start`, `thread/resume`, `thread/read`, `thread/list`, and `turn/start`
- event notifications over the same `stdio` connection

Implementation assumptions recorded here:

1. The wire format is newline-delimited JSON over `stdio`, as shown in the official examples.
2. The adapter only projects fields that are explicitly documented or observable in example payloads.
3. Some notification examples do not show `threadId` on every payload. In practice, some Codex events may identify the thread with `conversationId` instead. The registry treats either field as a thread identifier when present, and only falls back to previously known `turnId` mappings when neither identifier is available.
4. `listen` subscribes by resuming the thread first, because the event stream is defined for started or resumed threads.
5. `listen` is a message-first operator view: it reads recent thread history to print actual user and assistant messages before continuing with newly detected messages, marks the existing snapshot as seen so older backlog messages are not replayed again after resume, and falls back to periodic `thread/read` refreshes because some App Server connections may emit only status-style notifications for live updates.
6. `listen` and `listen-and-send` suppress token-by-token `item/agentMessage/delta` console rendering and instead rely on stable item payloads and periodic `thread/read` refreshes to show human-readable assistant messages.
7. `listen-and-send` uses the same listening pipeline as `listen`, but each non-empty line typed into stdin is sent as a fresh `turn/start` on the same thread. New turns use configured `turn_start_options` from the sidecar config so terminal input always follows the explicit approval and sandbox defaults.
8. In interactive terminals, `listen-and-send` keeps a stable bottom prompt and renders new thread output above it. In non-interactive stdin or test runs, it falls back to plain line reads.
9. When the App Server sends `item/commandExecution/requestApproval`, the CLI treats it as a server-initiated JSON-RPC request, prints the approval prompt to the terminal, and replies with an explicit approval decision such as `accept` or `cancel`.
10. App Server stderr output may contain internal warnings unrelated to the current thread, so it is suppressed at normal log levels and only surfaced through debug logging.
11. The current implementation is local-server-only. Remote App Server targets are out of scope unless the docs are updated explicitly.
12. Runtime configuration comes from the local TOML config file and code defaults. Environment-variable configuration is intentionally not supported, except for explicit config-file secret references such as `telegram.bot_token_env`.
13. Advanced or experimental surfaces such as dynamic tools, WebSocket transport, reviews, rollback, archive, and compaction are intentionally not implemented in the current scope.
14. Telegram is treated as an alternate local operator transport for `listen-and-send`, not as a separate workflow engine. The Codex App Server remains the only live source of thread and turn truth.
15. The initial Telegram implementation uses Telegram Bot API polling from the local sidecar process and persists the bound chat ID plus the last consumed `update_id` in the local registry so the bridge can recover cleanly after restart.
16. If no explicit Telegram chat is configured, the initial authorized inbound Telegram message becomes the bound chat for that thread session. Outbound messages detected before that bind are buffered locally and flushed once the chat is attached.
17. Telegram approvals reuse the same decision parsing as terminal approvals: while approval is pending, messages such as `approve` and `cancel` are treated as approval responses instead of new turns.
