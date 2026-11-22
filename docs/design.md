# Design Notes

## Determinism boundary

The scheduler never reads the wall clock. Every ordering key is derived from persisted
logical fields:

```text
assignment: (window_end, projected_compute / compute_units, window_count,
             replica_id, replica_version)
requests:   (priority DESC, arrival_ms, seq)
windows:    (end_ms, start_ms, replica_id, replica_version)
batches:    (window_id, ordinal)
training:   (committed_clock_ms, window_id, ordinal)
```

JSON output uses `sort_keys=True`; the digest is computed from a compact canonical
encoding. The status payload contains no process IDs, wall-clock timestamps, random
UUIDs, or SQLite rowids.

## Window assignment

Each visible pending request scans replicas from all snapshots known to the store.
For every candidate replica it computes an aligned window:

```text
start = floor(max(arrival_ms, request.not_before_ms) / batch_window_ms)
        * batch_window_ms
end   = start + batch_window_ms
```

The version is selected from `effective_at_ms <= start`, not from the current wall
clock and not from the end of the window. A window retains this snapshot even if a
new version becomes current before the window closes.

If a window cannot accept more requests, the scan advances by one window. The stable
sort then chooses a single `(replica, version, window)` binding. `request_assignments`
has one row per request, while `assignment_history` retains evidence.

## Batch adjudication

The planner sorts requests by priority, arrival, and sequence. It greedily tries to
append each request to existing batches before opening another batch. Every addition
is checked against:

- replica `max_batch_size`;
- per-window `compute_units`;
- batch peak `memory_mib`;
- per-window batch slot limit.

The planner records the first constraint that prevented a request from joining each
batch. Requests not placed are either expired (when the window-effective clock is
past their deadline) or deferred with `not_before_ms = window.end_ms`. Deferred
requests are reassigned immediately after the window commit, before another window
is adjudicated. This removes invocation-boundary dependence from recovery.

## Fixed point and error bounds

Final metric values are converted to integer units with `Decimal(str(raw))`.
The conversion error is at most half a fixed-point unit. Floating multiply/add steps
also record a conservative half-ULP bound. For each metric the ledger stores:

```text
operation ordinal
operation kind
operand A
operand B
result
batch fixed value (for convert_fixed)
scale
rounding direction
propagated error bound
cumulative error bound
```

The final batch error bound is the cumulative conversion bound for that metric.
Budget checks use fixed integers, so precision flags describe real decisions rather
than post-hoc reporting.

## Precision sensitivity

The planner constructs a second deterministic plan in arrival order. If membership
differs and the estimated total latency differs by less than tolerance, a
`batch_plan` flag is emitted. Additionally, a selected/rejected request pair whose
compute estimates fall inside tolerance emits a `batch_candidate` flag.

Replica assignment compares the two closest fitting candidates. When their projected
compute differs by less than tolerance and the choice uses the stable
`replica_id` tie-break, a `replica_choice` flag is persisted.

All flags require `depended=true` and include `raw_a`, `raw_b`, `difference`, and
`tolerance`.

## Crash protocol

```text
window open
    |
    | transaction A
    v
window proposed + batches proposed + members + error ledger + flags
    |
    | transaction B
    v
window committed + batches committed + request BATCHED + resource_ledger
```

`--crash-at batch_decision` exits with code 86 immediately after transaction A.
`--crash-at window_commit` exits with code 87 inside transaction B after row updates
but before `COMMIT`.

Recovery:

1. Finalize all `proposed` windows in chronological order.
2. Reassign pending/deferred requests.
3. Continue with open windows.

Resource totals are never decremented from an in-memory budget. They are recomputed
from `resource_ledger` with `SUM`/`MAX`, and each batch can insert at most one ledger
row. This makes retries idempotent.

## SQLite and locking

Each writer:

1. acquires a cross-platform advisory file lock;
2. opens SQLite with `journal_mode=WAL`, `synchronous=FULL`, and a busy timeout;
3. uses `BEGIN IMMEDIATE` for write transactions.

Primary keys and unique constraints independently enforce:

- one window row per `(replica_id, version, start, end)`;
- one current assignment per request;
- one batch membership per `(request_seq, replica_version)`;
- at most one resource ledger row per batch;
- idempotent lifecycle evidence keys.

## Failure and expiry

`EXPIRED` is a scheduling/execution deadline outcome. It increments `expiry_count`
but never `failure_count` or `consecutive_failures`.

A real injected execution failure marks the batch `failed`, increments request and
replica failure counters, and emits `execution_failure`. Re-running `train` skips
non-pending batches, so counters cannot be double-incremented.
