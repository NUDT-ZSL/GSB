# Design Notes

## Deterministic peer rule

The loopback peer grants flow-control windows as a pure function of the
received DATA sequence (never of wall-clock time), so the client planner
can predict every WINDOW_UPDATE byte-exactly:

- per stream and at connection level, track consumed-since-grant;
- after each DATA frame, emit `WINDOW_UPDATE(stream, consumed)` when
  `consumed >= grant_increment` **or** the mirrored client stream window
  is exhausted; then the same check at connection level;
- on stream end, flush the remaining consumed amount for that stream;
- grants become visible to the client `grant_latency_ms` (simulated
  ticks) after emission; the real peer emits them immediately on receipt.

The planner keeps two views: the client ledger (credited when a grant
*arrives*) and the peer mirror (credited when a grant is *emitted*). The
grant trigger is evaluated against the peer mirror — exactly what the
real peer does — which is what makes plan and runtime agree byte-for-byte.

## Scheduling strategies

- `weighted_rr`: smooth weighted round-robin (nginx-style); ties break by
  arrival index.
- `window_first`: largest remaining stream window first; ties by arrival.
- `arrival`: strict declaration (arrival) order.

Every frame is `min(max_frame_size, stream window, connection window,
remaining body)` — never above the frame-size limit, never above any
available window. Same input + same strategy => byte-identical plan
(verified by tests and by replay verification).

## Scale-adaptive selection

`policy.select_strategy` simulates every candidate on the declared input
and estimates the stall cost (sum of predicted window-exhaustion stall
milliseconds). The cheapest strategy wins. When candidates are within the
explicit `tie_threshold_ms`, the fixed priority order
`(weighted_rr, window_first, arrival)` decides — never dict/hash order.
The decision record (selected name, reason, key scale parameters, all
candidate costs, threshold) is written into the diagnostic report
together with the send plan.

## Window ledger and reconciliation

The ledger tracks per-stream and connection-level sent bytes, remaining
window and every WINDOW_UPDATE increment. Sending never drives a window
negative (a violation raises `E_WINDOW`). Stall segments (window
exhaustion) are recorded with monotonic-clock start/end milliseconds and
the affected stream ids. After a run, the predicted grant list is
reconciled item-by-item against the increments the peer actually wrote
back; the first deviation (stream id, expected vs observed bytes) is
reported as `E_WINDOW`.

## Fairness

For each stream: expected share (`total_bytes * effective_weight /
sum(effective_weights)`), actual bytes, deviation, and the maximum run of
consecutive scheduling rounds in which the stream was eligible but not
picked (starvation; flagged at >= 8 rounds). Explicit fallback rules:
weight <= 0 is treated as 1 (`weight_fallback`); a stream eligible while
its dependency parent was also eligible has its weight treated as 1
(`weight_ineffective_fallback`). Rows are sorted by stream id.

## Snapshots

A snapshot persists the manifest version, the full manifest, the policy
decision (with threshold), the window ledger, the send plan and the
diagnostics. Loading compares the snapshot manifest version with the
current session version: a mismatch replaces the whole session state
(manifest, ledger, plan) — never an incremental merge. Replay
verification recomputes the plan deterministically from the snapshot and
compares frame-for-frame and grant-for-grant; any divergence is
`E_REPLAY` with the first diverging frame index.

## GUI

Tkinter single-window workbench: frame timeline (HEADERS/DATA/
WINDOW_UPDATE/STALL lanes per stream plus a connection lane), per-stream
window-remaining curves, current policy + reason, fairness table,
diagnostics log and snapshot list. All rendering iterates explicitly
sorted collections (stream id, frame sequence), so the display order is
stable across runs.
