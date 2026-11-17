# Validation Record

Validation date: 2026-11-17 (Asia/Shanghai).

## Environment

```text
Python 3.11.0rc1
NumPy 2.4.6 (installed in .venv)
SQLite from Python standard library
Linux/WSL validation host; Windows batch files delivered for target usage
Tkinter not installed in this Linux image; GUI code is lazy-imported and its row model is tested
```

The environment's system Python lacked `ensurepip`; `.venv` was bootstrapped with
`--without-pip` plus the official `get-pip.py`, then `requirements.txt` was installed.
No Docker or container runtime was used.

## Automated tests

Command:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Result: **30 tests, all passed**.

Coverage includes:

- `E_SPEC` rejection for duplicate node ids, duplicate JSON keys, invalid digest,
  non-positive byte size, invalid priority, missing check references, and cycles;
- truth majority, strict-majority flag, 3:3 frequency tie resolved by priority,
  deterministic node-id tie ordering, and reversed replica-state order;
- floor quantization, ratio comparison error bound, ratio-alert flips, cost tie
  channel, source ordering flip, truth/mismatch grid invariants;
- loopback digest query, block get/put, atomic write, cold restart, and forced
  process kill followed by restart with the same persistent digest;
- `E_BLOCK`, `E_INTEGRITY`, and `E_GRID` paths and structured log keys;
- JSON + SQLite round trip, SQLite table presence, stale-view detection, whole-view
  replacement, read-only backups, and content-digest tamper detection;
- Tkinter-independent stable row ordering and all three demo experiments.

## Demo

Command:

```bash
.venv/bin/python -m replica_ledger.cli demo --workdir run/demo \
  --report-json reports/demo_report.json \
  --report-text reports/demo_report.txt
```

Invariants from the stable report:

```text
all_repairs_converged = true
all_ledgers_reopened_current = true
exact_truth_digest_and_mismatch_set_are_grid_invariant = true
node_connection_order_independent = true
lexical_hash_iteration_not_used_for_decisions = true
```

The report includes single-replica repair, 4-of-6 majority repair with two
mismatches, and grid perturbation. In the grid experiment the source changes from
`n1` to `n2` when the cost gap grows beyond the derived bound; ratio-alert certainty
also flips at the quantized threshold boundary. Each flip contains integer cost
terms, gap, bound, and `error_bound_share_ppm`.

The demo was run twice into the same workspace and generated report JSON/TXT files
were byte-for-byte identical.

Stable report digest: `08f960ebdf4e5531cffc0eb2e1b519792c0b9b41740d11f12dcbef401c7808b5`.

## Anomaly checks

Observed exit codes and paths:

| Injection | Code | Exit | Example field path |
| --- | --- | --- | --- |
| block file truncation | `E_BLOCK` | 3 | `nodes[3].blocks[0].byte_size` |
| shard digest corruption | `E_INTEGRITY` | 4 | `nodes[0].blocks[0].shards[0].digest` |
| invalid grid | `E_GRID` | 5 | `session.scale` |

Each corresponding JSONL error record contained non-empty `block_id`, `node_id`,
and `field_path` keys. Block truncation logs included expected/actual sizes; digest
corruption logs included expected/actual digests.

## Ledger recovery and replacement

Observed:

- first save -> `saved`; reopen with same state -> `current`;
- changed state -> reopen -> `stale`, no merge;
- reconcile -> `replaced`; both prior JSON and SQLite copied as mode `0444` files;
- new state reopen -> `current`;
- tampering with payload without updating sealed content digest -> `E_LEDGER`.

## Delivered artifacts

- `samples/scenarios/{single,multi,grid}` deterministic generated groups.
- `reports/demo_report.json` and `reports/demo_report.txt`.
- `scripts/*.bat` for Windows setup, generation, tests, demo, ports, node start/stop,
  and GUI launch.
- No container files or network-dependent sample inputs.

Machine-readable summary: `reports/validation_summary.json`.
