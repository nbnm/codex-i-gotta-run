# Assumptions

This prototype implements the Codex App Server integration against the currently documented stable JSON-RPC flow:

- one `initialize` request followed by an `initialized` notification per connection
- `thread/start`, `thread/resume`, `thread/read`, `thread/list`, `turn/start`, `turn/steer`, `turn/interrupt`, and `thread/unsubscribe`
- event notifications over the same `stdio` connection

Implementation assumptions recorded here:

1. The wire format is newline-delimited JSON over `stdio`, as shown in the official examples.
2. The prototype only projects fields that are explicitly documented or observable in example payloads.
3. Some notification examples do not show `threadId` on every payload. When a live event omits `threadId`, the registry keeps the raw event and only infers the thread from a previously known `turnId` when that mapping already exists.
4. `turn/steer` is implemented with `expectedTurnId`, matching the documented contract.
5. `tail` subscribes by resuming the thread first, because the event stream is defined for started or resumed threads.
6. Advanced or experimental surfaces such as dynamic tools, WebSocket transport, reviews, rollback, archive, and compaction are intentionally not implemented in v0.

