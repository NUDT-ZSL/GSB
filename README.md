# Slow Query Sampling and Plan-Profile Engine

This repository implements a deterministic, local-only slow-query sampling and
execution-plan profiling engine. It accepts fixed JSON samples and a fixed
SQLite database, normalizes SQL into query templates, aggregates samples,
profiles candidate access paths, and incrementally invalidates profiles when a
statistics snapshot is refreshed.

The engine uses only Python 3.10+ standard-library modules. It does not read the
system wall clock and does not require a container or network access.

## Quick Start

```bash
./run_tests.sh
./demo.sh
```

`demo.sh` clears `results/demo`, runs the complete workflow twice from the same
fixed samples, injects one hard process exit at a journal boundary in each run,
resumes it, and compares the two `status.json`/`verify.json` outputs byte for
byte.

Manual CLI:

```bash
python3 -m slowplan load   --db results/state.sqlite
python3 -m slowplan ingest --db results/state.sqlite --samples samples/slow_queries.json
python3 -m slowplan profile --db results/state.sqlite
python3 -m slowplan apply  --db results/state.sqlite --patch samples/stats_patch_v2.json
python3 -m slowplan ingest --db results/state.sqlite --samples samples/slow_queries_after_refresh.json
python3 -m slowplan profile --db results/state.sqlite
python3 -m slowplan status --db results/state.sqlite
python3 -m slowplan verify --db results/state.sqlite
```

All commands emit stable, key-sorted JSON. `profile` and `resume` include the
full current conclusions: candidate paths, estimates, measured means, deviation,
selected path, precision-sensitive evidence chains and aggregate summaries.
`resume` replays only journaled operations left in `running` state.

## Repository Layout

- `slowplan/normalize.py`: SQL tokenizer, literal/IN-list normalization, template ID and query structure.
- `slowplan/samples.py`: atomic sample-stream validation.
- `slowplan/stats.py`: sample-database catalog digest, snapshot validation, patch validation.
- `slowplan/aggregate.py`: fixed-order aggregation and linearly interpolated percentiles.
- `slowplan/profiling.py`: candidate paths, estimates, costs, deviation, precision-sensitive evidence chain.
- `slowplan/dependencies.py`: statistics -> index/access -> template -> profile dependency graph and closure.
- `slowplan/store.py`: SQLite schema, pointers, operations journal, state digest.
- `slowplan/engine.py`: load/ingest/apply/profile/resume/status/verify orchestration.
- `slowplan/cli.py`: command-line interface and business-error exit codes.
- `samples/build_sample_db.py`: deterministic sample database and JSON regeneration.
- `samples/sample_db.sqlite`: fixed initial sample database.
- `samples/slow_queries*.json`: ordered sample streams and negative stream.
- `samples/stats_*.json`: initial snapshot, refresh patches and invalid examples.
- `tests/`: normalization, aggregation, workflow, failure, recovery and concurrency tests.

Regenerate all fixed samples:

```bash
python3 samples/build_sample_db.py
```

The sample database digest is computed from sorted schema, indexes and rows, so
canonical sample data is deterministic even though raw SQLite file bytes are not
part of the contract.

## Deterministic Rules

### SQL normalization and aggregation

- String and numeric literals become `?`.
- Any literal/parameter `IN` list becomes `IN (?)`; list length does not change the template.
- Keywords are uppercase, identifiers are lowercase, and whitespace is canonical.
- `template_id = "tpl_" + sha256(template_sql)[:20]`.
- Samples are sorted by `sample_seq` before every sum, mean or percentile operation.
- Percentiles use linear interpolation at position `(n-1) * p` and round half-up to 6 decimals.
- Scan amplification is `sum(rows_scanned) / sum(rows_returned)`, rounded to 6 decimals; it is `null` when no rows were returned.
- Windows use `floor(collected_at_ms / window_ms) * window_ms`.

The supported normalizer subset is single-table `SELECT` with `AND` predicates,
optional `ORDER BY` and `LIMIT`. `OR`, joins, unions and non-SELECT statements
are rejected as `E_SQL_UNNORMALIZABLE`.

### Plan profiles

Each template profile at a statistics version contains:

- A full-table-scan candidate.
- One candidate for every index whose leading column is constrained by `=` or `IN`.
- Integer estimated rows, six-decimal selectivity/cost, measured mean scanned rows,
  `measured_mean / estimated_rows` deviation ratio and direction
  (`underestimate`, `overestimate`, `aligned`).
- The selected path using `(estimated_cost, path_id)` ordering.
- `precision_sensitive=true` when the cost gap to the best alternative is less
  than the supplied tolerance. Both paths then include an error-source chain with
  statistics item keys, table/column/index names and conversion steps.

The estimator is intentionally deterministic and inspectable rather than a copy
of a production optimizer. Index cost is `estimated_rows + 2`; full scan cost is
the table row count. Equality/IN filter selectivity comes from column NDV and
uses a canonical IN cardinality of 3; range predicates use a fixed `1/3` factor.

If the selected path estimates zero rows while measured scans are positive, the
template produces `E_ZERO_ESTIMATE`. Other templates in the same profile run are
still committed.

### Statistics activation and incremental invalidation

- A snapshot is committed when its patch is accepted, but only becomes active
  when the injected logical clock reaches `effective_at_ms`.
- A sample belongs to the newest snapshot whose effective time is at or before
  the sample's `collected_at_ms`.
- Applying a future-effective patch changes `committed_stats_version`, not
  `active_stats_version`, and does not touch current profiles.
- On activation, the engine compares actual statistics items between adjacent
  snapshots and walks the persisted directed graph:
  `statistics item -> index/access path -> query template -> profile conclusion`.
- Only templates reachable from changed items lose their current pointers.
  Unaffected profile rows and conclusion bytes are not copied or rewritten.
- Each invalidation records all triggers, old/new values and propagation paths.
- A patch with a rolled-back/invalid target version is rejected with
  `E_STAT_VERSION_ROLLBACK`; a content conflict on an existing version returns
  `E_STAT_VERSION_CONFLICT`.

### Persistence, recovery and concurrency

- SQLite uses WAL, foreign keys, full synchronous writes and 30-second busy timeout.
- All mutating operations additionally take an advisory `flock` on `<state>.lock`.
- Every ingest/apply/profile first commits an operation-journal row, then writes
  the result and `completed` marker in one transaction. A crash leaves only a
  `running` row and no partial domain pointers.
- Profile refresh computes into temporary stage tables and atomically transfers
  rows while replacing `current_profiles` pointers.
- `sample_seq` and `(template_id, stats_version, profile_version)` uniqueness
  prevent duplicate sample, snapshot and profile commits.
- Duplicate sample batches, patches and identical profile refreshes are
  idempotent. Backfilling an older, non-conflicting sequence is supported so
  independently scheduled sampling workers can finish in any order.
- Dependency-graph version is derived from the canonical edge set, so final
  state does not depend on worker scheduling order.
- Readers open a read-only SQLite snapshot and never observe staged profile rows.

## Business Error Codes

| Code | Exit | Meaning |
| --- | ---: | --- |
| `E_BAD_SAMPLE` | 2 | Invalid sample field/type/range |
| `E_BAD_STAT` | 3 | Invalid statistics snapshot or patch |
| `E_DUP_TEMPLATE` | 4 | Template ID bound to different normalized structure |
| `E_SQL_UNNORMALIZABLE` | 5 | SQL outside supported deterministic grammar |
| `E_STAT_VERSION_CONFLICT` | 6 | Base/target version or content conflict |
| `E_STAT_VERSION_ROLLBACK` | 7 | Target version does not advance |
| `E_UNKNOWN_STAT_COLUMN` | 8 | Patch references a missing column |
| `E_DB_DIGEST_MISMATCH` | 9 | Snapshot database digest differs from sample DB |
| `E_ZERO_ESTIMATE` | 10 | Selected plan estimates zero while measured scan is positive |
| `E_CLOCK_ROLLBACK` | 11 | Injected clock moved back beyond tolerance |
| `E_NOT_INITIALIZED` | 12 | State database is absent |
| `E_SAMPLE_CONFLICT` | 13 | Same `sample_seq` with different content |
| `E_SAMPLE_NOT_MONOTONIC` | 14 | Input stream sequence is not strictly increasing |
| `E_STAT_EFFECTIVE_ROLLBACK` | 15 | Patch collection/effective clock regresses |
| `E_UNKNOWN_STAT_INDEX` | 16 | Patch references a missing index |
| `E_MISSING_STAT` | 17 | Query references unavailable statistics |
| `E_ALREADY_INITIALIZED` | 18 | State baseline differs from requested load |
| `E_VERIFY_FAILED` | 19 | Persisted state/output consistency check failed |

Invalid inputs are fully validated before an operation journal row or domain
write. `profile` can return exit 10 while still committing valid templates from
the same run; its JSON contains `"partial": true`, a top-level `error` mirroring
the first failure, and per-template `errors`.

## Fixed Sample Scenario

The database has four tables, five explicit indexes and fixed rows. Four query
templates are exercised:

- `orders.customer_id = ?`
- `orders.status = ? AND orders.created_at >= ?`
- `products.category_id IN (?)`
- `feature_flags.enabled = ?`

Version 2 changes `orders.status` NDV, `idx_orders_status` prefix selectivity and
`idx_flags_enabled` prefix selectivity. Activation therefore invalidates exactly
the status and feature-flag templates. The customer and product template profile
IDs and conclusion JSON remain byte-identical until new v2 samples themselves
require an aggregate refresh.

The negative files cover:

- `slow_queries_invalid.json`: unterminated SQL.
- `stats_patch_rollback.json`: version rollback.
- `stats_patch_unknown_column.json`: missing statistics column.
- `stats_snapshot_bad_digest.json`: sample database digest mismatch.
- `stats_patch_zero_estimate.json` plus `slow_queries_zero_case.json`: zero estimate with positive measured scan.

## Actual Verification

Recorded on 2026-11-21 in this workspace:

- `python3 -m unittest discover -s tests -v`: **19 tests passed**.
- Full CLI workflow: active snapshot `2`, committed snapshot `2`, sample
  watermark `16`, logical clock `3300`, exactly 2 invalidations, 0 pending after
  refresh, and `verify=true`.
- Hard-exit recovery at both `apply_after_journal` and
  `profile_after_journal`: exit code `91`; after `resume`, `status` JSON and
  `state_digest` equal the uninterrupted run field-for-field.
- Six simultaneous duplicate ingest/apply/profile processes each left one
  operation row, one copy of each sample/snapshot and a valid state.
- The same 10 initial and 6 refreshed samples ingested in opposite batch order
  produced identical `status` JSON and verified state.
- `demo.sh` two independent runs produced byte-identical `status.json` and
  `verify.json`. The verified state digest is
  `1ce407fb8b44729f720c8bf48ec3ce06cf632132d8efca1e9c7b7f66cf319198`
  and the status digest is
  `c8cb9a7c7f67c3fadf213c255932ffbde0b627a088bfa8682a523bba5ef15cea`.
