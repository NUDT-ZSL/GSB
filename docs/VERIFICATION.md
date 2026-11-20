# Verification record

## Environment actually used

```text
Python 3.10.12
SQLite 3.37.2
NumPy 1.26.4
Linux 6.x (WSL2)
```

The requested target is Python 3.11. Python 3.11 was not installed in the execution image; the implementation uses 3.10-compatible syntax and is intended to run unchanged on 3.11. Windows-specific file locking is implemented with `msvcrt`, but native Windows execution was not available in this container.

## Automated tests

Command:

```bash
pytest
```

Observed result:

```text
..........................                                               [100%]
26 passed in 3.24s
```

The suite covers:

- v1 duplicate IDs, adjacency disagreement, invalid weight/time, invalid linear index;
- package node bounds, bad shard hashes and missing shards;
- direct v2 shard content-hash tampering;
- v2 tombstone/state inconsistency;
- read-only merge preview with no persistent state;
- v1/v2 merge arbitration and source/decision traceability;
- exact tombstone audit, label-prefix filtering and two-hop BFS truncations;
- per-edge weight/label filtering without accumulation;
- distinct query errors and recovery after an error;
- injected clock rollback rejection during traversal;
- no-op, append and rollback/clock-rollback replacement classification;
- explicit downgrade and historical committed-version pointer restoration;
- hard kill after a traversal layer checkpoint and exact resumed output;
- hard kill during index staging, old-pointer visibility and resumed build;
- result-digest corruption detection;
- multi-process replacement and traversal idempotency;
- concurrent replacement with distinct traversal batches.

Sample reproducibility check:

```text
sample_rebuild_identical: True
sample_file_count: 14
```

`python3 scripts/build_samples.py` was run twice; all 14 generated JSON/SQLite sample files had identical SHA-256 values.

## Acceptance script

Command:

```bash
python3 scripts/run_acceptance.py --output docs/verification-result.json
```

Observed canonical JSON:

```json
{"checks":{"append_classification":"incremental_append","bad_shard_error":"GS_SHARD_HASH_MISMATCH","baseline_result_digest":"707d1e0ee5f2b244a5ff35ebd3515639856b82b37200f52bdc1e12675270fc5a","crash_exit_code":-9,"replace_crash_exit_code":-9,"replace_recovered_node_count":10,"resume_equal_to_baseline":true,"rollback_error":"GS_VERSION_ROLLBACK","tombstone_truncations":2,"traversal_committed_batches":1,"verify_ok":true},"ok":true,"sample_index":{"edge_count":9,"index_version":1,"node_count":9,"tombstone_count":2}}
```

Key acceptance facts:

- Base merge: 9 nodes, 9 edges, 2 tombstones.
- Two-hop traversal: 3 emitted nodes, 2 tombstone truncations, 4 visited nodes.
- The child traversal process was killed with exit code `-9` after layer 1 was committed.
- Resume output was field-for-field identical to the uninterrupted baseline.
- Exactly one traversal watermark was committed after restart.
- The replacement child process was killed with exit code `-9`; the old 9-node index remained visible while the 10-node build was pending.
- Repeating the same replacement resumed the build to 10 nodes and produced a valid current index.
- Version rollback produced `GS_VERSION_ROLLBACK`.
- Corrupt shard content/file hash produced `GS_SHARD_HASH_MISMATCH`.
- `verify` returned `ok: true`.

## Manual command trace

```bash
python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  load --package samples/packages/base/manifest.json

python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  traverse --batch-id two-hop --kind reachable --node-id n1 --depth 2

python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  status

python3 -m graphstore.cli --db data/graphstore.sqlite3 --clock samples/clock.json \
  verify --batch-id two-hop
```

The checked-in `docs/verification-result.json` is the exact stdout of the acceptance script.

