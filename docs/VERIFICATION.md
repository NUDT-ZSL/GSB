# Verification Record

Verification was performed locally on 2026-11-23 with CPython 3.12.13 and the
checked-in deterministic samples. No external network target was used.

## Automated Tests

Command:

```bash
PYTHONPATH=src python3 -m pytest -q
```

Observed result:

```text
.........................                                                [100%]
27 passed
```

Coverage includes:

- path traversal and ETag/body validation;
- ETag forms, Cache-Control minimum TTL, and cache generation rollback;
- unknown injection rejection before state creation;
- v2 shard hash mismatch;
- v1 relative-seconds conversion trace;
- arbitration determinism over all input permutations;
- logical clock/version/format tie-break behavior;
- fresh/boundary/expired classification;
- sampled-mode bound and deterministic bucket selection;
- expired-first deterministic eviction;
- normal 304/200, malformed headers, timeout, and connection close;
- full injected round;
- same-run-id idempotency;
- index version rollback and run-id plan conflict rejection;
- excessive clock rollback with unchanged committed index;
- crash after prepare and crash inside apply, followed by recovery;
- two concurrent CLI processes committing one version.

## Full Deterministic Round

Command:

```bash
PYTHONPATH=src python3 -m cdn_edge_workbench run \
  --samples samples \
  --state <temporary-state> \
  --run-id final-check \
  --compact
```

Observed summary:

```json
{
  "active_arbitrations": 1,
  "algorithm_exact": 1,
  "algorithm_issues": 0,
  "algorithm_sampled": 5,
  "audits": 7,
  "boundary": 2,
  "entries": 8,
  "evicted": 2,
  "expired": 5,
  "failed_origin": 4,
  "fresh": 1,
  "freshness_records": 8,
  "index_version": 1,
  "refreshed_304": 1,
  "replaced": 2,
  "revalidated": 7,
  "snapshot_arbitrations": 12,
  "snapshot_candidates": 25,
  "validation_budget_skipped": 0
}
```

Observed audit code counts:

```json
{
  "E1201_ORIGIN_STALE_304": 1,
  "E1202_ORIGIN_UNCHANGED_200": 1,
  "E1204_ORIGIN_REQUEST_TIMEOUT": 1,
  "E1205_ORIGIN_MALFORMED_HEADERS": 1,
  "E1301_INJECTED_CONTENT_TAMPER": 1,
  "E1302_INJECTED_ETAG_CONFLICT": 1,
  "E1303_INJECTED_CLOCK_ROLLBACK": 1
}
```

Observed successful paths:

- `/data/report.json` on `edge-b`: valid 304, cache generation remained `2`.
- `/feeds/weather.json` on `edge-a`: valid 200, cache generation changed
  `2 -> 3`.
- `/feeds/news.json` on `edge-b`: valid 200, cache generation changed `2 -> 3`.

Observed evictions:

- `edge-a /assets/style.css`: expired-first was not applicable after
  revalidation, so oldest logical verification was selected.
- `edge-b /assets/app.js`: expired entry selected first.

Observed algorithm decisions:

- `edge-a`: sampled digest buckets, trigger threshold exceeded.
- `edge-b`: sampled digest buckets, trigger threshold exceeded.
- `edge-c`: exact per-entry, below threshold.

## Crash and Recovery

Prepare-phase crash:

```text
exit code: 70
committed pointer: 0
committed audit count: 0
uncommitted audit count: 7
run journal status: prepared
```

After `recover`:

```json
{
  "actions": [
    {"action": "detected_uncommitted_audit", "runs": {"crash-run": 7}},
    {"action": "replayed_prepared_run", "index_version": 1, "run_id": "crash-run"}
  ],
  "committed_version": 1,
  "entry_count": 8,
  "audit_count": 7
}
```

After recovery, the uncommitted audit count was `0`, the replacement ledger
contained `2` rows, and the journal status was `committed`.

A separate `during_apply` fault left the pointer at version `0` and seven
prepared audit rows. Recovery published version `1` with eight entries and seven
audits.

## Clock Rollback

Requested logical clock `1190` after a committed clock `1200`, with tolerance
`5`, returned:

```json
{
  "ok": false,
  "error": {
    "code": "E1101_CLOCK_ROLLBACK",
    "details": {
      "last_logical_clock": 1200,
      "requested_logical_clock": 1190,
      "tolerance_ticks": 5
    }
  }
}
```

The committed index version, entry list, and audit list were unchanged.

## Multi-Process Idempotency

Two CLI processes were launched concurrently with the same state and
`--run-id concurrent-run`.

Observed:

- both processes exited `0`;
- statuses were one `committed` and one `idempotent_existing_commit`;
- committed index version was `1`;
- one run-journal row existed;
- replacement-ledger row count was `2`;
- committed audit count was `7`.

## Sample Immutability

The engine test hashes every file under `samples/` before and after a full
injection round. The hashes are identical, confirming that parse, merge,
freshness, and origin injection only mutate in-memory/network behavior.

