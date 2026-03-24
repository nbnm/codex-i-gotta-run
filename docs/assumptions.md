# Assumptions

This prototype implements the Codex App Server integration against the currently documented stable JSON-RPC flow:

- one `initialize` request followed by an `initialized` notification per connection
- `thread/start`, `thread/resume`, `thread/read`, `thread/list`, and `turn/start`
- event notifications over the same `stdio` connection

Implementation assumptions recorded here:

1. The wire format is newline-delimited JSON over `stdio`, as shown in the official examples.
2. The prototype only projects fields that are explicitly documented or observable in example payloads.
3. Some notification examples do not show `threadId` on every payload. In practice, some Codex events may identify the thread with `conversationId` instead. The registry treats either field as a thread identifier when present, and only falls back to previously known `turnId` mappings when neither identifier is available.
4. `listen` subscribes by resuming the thread first, because the event stream is defined for started or resumed threads.
5. `listen` is a message-first operator view: it reads recent thread history to print actual user and assistant messages before continuing with newly detected messages, marks the existing snapshot as seen so older backlog messages are not replayed again after resume, and falls back to periodic `thread/read` refreshes because some App Server connections may emit only status-style notifications for live updates.
6. `listen` and `listen-and-send` suppress token-by-token `item/agentMessage/delta` console rendering and instead rely on stable item payloads and periodic `thread/read` refreshes to show human-readable assistant messages.
7. `listen-and-send` uses the same listening pipeline as `listen`, but each non-empty line typed into stdin is sent as a fresh `turn/start` on the same thread. New turns use configured `turn_start_options` from the sidecar config so terminal input always follows the explicit approval and sandbox defaults.
8. When the App Server sends `item/commandExecution/requestApproval`, the CLI treats it as a server-initiated JSON-RPC request, prints the approval prompt to the terminal, and replies with an explicit approval decision such as `accept` or `cancel`.
9. App Server stderr output may contain internal warnings unrelated to the current thread, so it is suppressed at normal log levels and only surfaced through debug logging.
10. Advanced or experimental surfaces such as dynamic tools, WebSocket transport, reviews, rollback, archive, and compaction are intentionally not implemented in v0.
