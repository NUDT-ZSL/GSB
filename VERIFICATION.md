# Verification Record

All results below were produced locally from the deterministic `samples/` directory. No container, network service, external data, or wall clock is used by engine semantics.

## Environment

```text
Python 3.11.15
SQLite 3.53.1
NumPy 2.4.6
```

Virtual environment setup used:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## Automated tests

Command:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Actual result:

```text
Ran 17 tests in 14.869s

OK
```

Covered suites:

- `test_validation.py`: closed enums, duplicate columns, clock ordering, invalid-init no database.
- `test_mvcc.py`: RR/RC, own-write merging, rollback, delete/rebuild, lost-update conflict, GC of rolled-back versions.
- `test_gc.py`: retained-view protection, concurrent GC/query consistency, NumPy equality, integrity.
- `test_recovery.py`: forced process exit after a staged insert, orphan cleanup, half-written detection, idempotent resume, four-process commit.
- `test_cli.py`: end-to-end commands, async GC, stale snapshot, query budget, version digest mismatch.

## Sample replay

Command:

```powershell
python -m mvcc_app init --table samples/table.json --config samples/config.json --sessions samples/sessions.json --replay
```

Key output:

```json
{
  "events": 46,
  "last_clock_ms": 2000,
  "transactions": {"active":0,"committed":8,"pending":0,"rolled_back":2}
}
```

Pre-GC status:

```json
{
  "version_chains": {"rows":7,"total_versions":14,"max_chain_length":3},
  "read_views": {"total":20,"retained":20,"expired":0},
  "gc": {"collected_total":0,"runs":0,"watermark_txn":1000}
}
```

## Visibility evidence

Stored range-read results from the sample:

| Read | View clock | Visible `id:balance` | Interpretation |
| --- | ---: | --- | --- |
| `txn:102:op:1` | 1060 | `1:100` | RC does not yet see active txn 101's update |
| `txn:102:op:3` | 1310 | `1:150, 3:350` | RC sees txn 101 after commit 1300 |
| `txn:101:op:3` | 1000 | `1:150` | RR sees its own later write |
| `txn:105:op:1` | 1460 | `1:150, 3:350, 5:500, 6:650` | Rolled-back txn 103's `id=5` update is invisible |
| `txn:108:op:1` | 1750 | `3:350` | RR still sees old `id=3` while delete txn 107 is active |
| `txn:109:op:1` | 1920 | empty | RC sees committed delete of `id=3` |

Same-transaction merge evidence for `id=6`:

```text
version 5: created_by=101, deleted_by=105, merge_count=1, balance=650
```

The insert and update by transaction 101 produced one version; `merge_count=1` records the merged update.

## GC evidence

Command:

```powershell
python -m mvcc_app gc --clock-ms 2200
```

Actual output:

```json
{
  "clock_ms":2200,
  "collected_count":4,
  "collected_version_ids":[7,8,9,13],
  "logical_latency_ms":11,
  "retained_count":10,
  "reason_counts":{
    "future_snapshot_visible":5,
    "row_head_live":5,
    "visible_to_read_view":10
  },
  "run_id":"gc:2200:1",
  "watermark_txn":1000
}
```

Post-GC status:

```json
{
  "version_chains":{"rows":5,"total_versions":10,"max_chain_length":3},
  "read_views":{"total":20,"retained":20,"expired":0},
  "gc":{"collected_total":4,"runs":1}
}
```

The collected versions are below transaction watermark 1000 and were not visible to any retained view or future snapshot. The status JSON exposes every retained version, its primary reason, and referencing view ids.

## Determinism verification

Command:

```powershell
python -m mvcc_app verify --runs 2 --gc-clock-ms 2200
```

Actual result:

```json
{
  "current_matches_replay":true,
  "deterministic":true,
  "numpy_batch_equal":true,
  "artifact_digest":"9bd1715a2e7178e7868a881f3977569022e70a0a073fc4917f53a55e3073f4bc",
  "version_count":10,
  "query_count":8,
  "gc":{"collected_total":4,"runs":1,"watermark_txn":1000}
}
```

Two independent temporary databases produced byte-identical semantic artifacts. NumPy object-array comparison of sorted version digests also returned true.

## Interrupt recovery

Actual sequence:

```text
edit --crash-at-event 2  -> process exit 91
resume #1                 -> exit 0
resume #2                 -> exit 0
```

First resume:

```json
{
  "recovery":{
    "cleaned":1,
    "orphan_generations":[3],
    "half_written_versions":1,
    "half_written_transactions":[100,101,102,103,104,105,106,107,108,109]
  },
  "missing_steps":{"start":9,"operation":26,"finish":10},
  "committed_transactions":[100,101,102,104,105,107,108,109]
}
```

Second resume:

```json
{
  "recovery":{"cleaned":0,"half_written_versions":0,"orphan_generations":[]},
  "missing_steps":{"start":0,"operation":0,"finish":0}
}
```

The version count before and after the second resume was unchanged. This confirms missing-only replay and no duplicate commit.

## Multi-process idempotent commit

The recovery suite starts one active transaction with one staged insert, then launches four separate Python processes that all execute:

```text
edit --txn-id 100 --finish --clock-ms 1200
```

Actual assertions:

- all four processes exit 0;
- transaction 100 has exactly one committed state;
- exactly one `txn:100:finish` idempotency record exists;
- exactly one version created by transaction 100 exists.

## Error-code evidence

Automated CLI assertions verified:

| Scenario | Code | Exit |
| --- | --- | ---: |
| Invalid duplicate column during init | `DUPLICATE_COLUMN` | 2 |
| Unknown transaction on read | `UNKNOWN_TRANSACTION` | 2 |
| Logical clock rollback beyond tolerance | `CLOCK_REGRESSION` | 2 |
| Expired retained view with retention 1 | `STALE_SNAPSHOT` | 2 |
| Query cost over configured budget | `QUERY_LATENCY_BUDGET_EXCEEDED` | 2 |
| Persisted content changed without digest update | `VERSION_DIGEST_MISMATCH` | 2 |

In the invalid-init case, the target database file was checked and did not exist after failure.

