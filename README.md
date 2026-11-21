# FlowAgg

FlowAgg is a local, deterministic loopback-traffic aggregation engine. It reads
JSONL flow batches, aggregates them under a hard memory budget, spills cold group
state to an atomic on-disk delta chain, reads that state back for later batches,
and commits every batch in a single SQLite transaction.

The engine is also an integrable Python component: `flowagg.plan.load_plan`,
`flowagg.Engine`, `flowagg.FlowAggError`.

## Quick Start

The workspace already contains an isolated Python 3.11 environment in `.venv`.
To recreate it:

```bash
uv python install 3.11
uv venv --python 3.11 .venv
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -r requirements.txt
```

Generate the fixed samples and run the full two-run plus crash-resume demo:

```bash
.venv/bin/python scripts/generate_samples.py
.venv/bin/python scripts/run_demo.py
.venv/bin/python -m unittest discover -v
.venv/bin/python scripts/platform_check.py
```

The demo writes isolated databases and reports under `artifacts/demo_run_1`,
`artifacts/demo_run_2`, and `artifacts/demo_crash_resume`. It fails unless all
four normal outputs are byte-identical across the two runs and the recovered
`status`/`verify` outputs are byte-identical to the uninterrupted run.

## Record and Plan

One JSON object per line:

```json
{"record_seq":1,"clock_ms":1000,"src_endpoint":"lo:127.0.0.1:41000","dst_endpoint":"lo:[::1]:42000","protocol":"TCP","direction":"ingress","bytes":512,"packets":4}
```

`record_seq` and `clock_ms` are non-negative integers. Sequence numbers are
strictly increasing within a batch and beyond the committed maximum across
batches. Endpoints must use a loopback host and optionally a port, or the
`lo-<id>`/`loopback-<id>` form. Protocol and direction must belong to the
closed enums declared by the plan. Byte and packet counts are non-negative
integers bounded by signed 64-bit arithmetic.

An aggregation plan:

```json
{
  "plan_id": "loopback-demo-v1",
  "dimensions": ["endpoint_pair", "protocol", "direction"],
  "protocols": ["TCP", "UDP", "QUIC"],
  "directions": ["ingress", "egress"],
  "key_normalization": {
    "endpoint_pair_order": "ordered",
    "protocol_case": "upper",
    "direction_case": "lower",
    "key_separator": "|"
  },
  "memory_budget_mib": 0.03,
  "read_buffer_fraction": 0.25,
  "max_batch_rows": 64,
  "spill_dir": "spill",
  "fixed_scale": 1000,
  "quantiles": [0.5, 0.9, 0.99],
  "max_clock_regression_ms": 500
}
```

The ordered `dimensions` array is the grouping hierarchy; any non-empty
combination of `endpoint_pair`, `src_endpoint`, `dst_endpoint`, `protocol`,
and `direction` is accepted. The canonical group key is compact JSON. A
separator-based display key is also emitted.

## CLI

```bash
.venv/bin/python -m flowagg load --plan samples/plan.json --db state.db
.venv/bin/python -m flowagg aggregate --plan samples/plan.json --db state.db \
  --manifest samples/manifest.json
.venv/bin/python -m flowagg resume --plan samples/plan.json --db state.db \
  --manifest samples/manifest.json
.venv/bin/python -m flowagg status --plan samples/plan.json --db state.db \
  --with-groups
.venv/bin/python -m flowagg verify --plan samples/plan.json --db state.db \
  --manifest samples/manifest.json
```

All successful output and all `E_xxx` errors are stable, sorted JSON. Commands
accept `--output PATH` for an atomic output write. Exit code `0` means success;
business error classes use `2` (input/plan), `3` (state/verification), `4`
(digest/clock), `5` (memory), `6` (locking/concurrency), and `7`
(persistence/internal).

## Guarantees

- Active group accounting and the read buffer reserve both come out of one
  hard MiB budget. There is no lossy eviction and no approximate count.
- Cold groups are serialized as additive delta shards, fsynced, and installed
  with same-directory `os.replace`; SQLite records the digest and chain link.
- A batch intent is validated and persisted first. Group states, sample series,
  group versions, spill manifest, watermarks, and consumed sequences then
  commit in one SQLite transaction.
- A killed process leaves at most a `processing` intent plus unreferenced
  spill/temp files. `resume` removes those files and redoes the uncommitted
  batch; committed groups are never partially visible.
- Duplicate `batch_id` with the same digest is idempotent. A different digest
  is rejected with `E_DIGEST_MISMATCH` before the watermark moves.
- Group memory is reset between batches and watermarks are read from SQLite,
  never inferred from process cache. This makes normal and recovered runs
  deterministic.
- Quantiles use fixed ordering (`value`, then `clock_ms`, then `record_seq`)
  and type-7 linear interpolation. Float and fixed outputs are both emitted;
  fixed output records scale, `half_up` rounding, raw interpolated value, and
  anchor values.
- A state-directory advisory lock plus SQLite `BEGIN IMMEDIATE`/WAL serializes
  command critical sections across processes, so group versions and spill
  paths cannot be doubly committed.

See `docs/DESIGN.md` for the storage model and commit protocol,
`docs/ERROR_CODES.md` for the error contract, and
`artifacts/VERIFICATION.md` for measured results.

## Windows Portability

The same-directory atomic rename uses `os.replace`, which maps to an atomic
replace on Windows volumes. Advisory locking uses `msvcrt.locking` on Windows
and `fcntl.flock` elsewhere; SQLite's own WAL/`BEGIN IMMEDIATE` lock is used in
addition. `scripts/platform_check.py` exercises atomic replace, SQLite
exclusion, and the advisory lock and is intended to be run on every target OS.
The checked-in result was produced on the Linux workspace
(`windows_runtime: false`); Windows-specific branches are present but were not
executed in this environment.
