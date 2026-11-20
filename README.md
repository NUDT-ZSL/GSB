# Local Schema Migration & Compatibility Diagnostics Workbench

一个仅依赖 Python 标准库、可在 Windows/Linux 本地运行的 SQLite 模式迁移工作台。它把多版本模式样本物化为不可变 SQLite 快照，按受控操作序列增量迁移，并对查询模板、视图、触发器和索引给出可追溯的失效闭包与精度诊断。

## Quick Start

```bash
# Python 3.11+ is recommended. Verified with a Python 3.12.13 virtual environment.
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

python -c "import sqlite3; print(sqlite3.sqlite_version)"
python -m schema_migrator --workspace .schema-workbench load
python -m schema_migrator --workspace .schema-workbench migrate --all
python -m schema_migrator --workspace .schema-workbench status
python -m schema_migrator --workspace .schema-workbench verify
```

No network access is required. The runtime has no third-party dependencies. `requirements.txt` is intentionally empty of runtime packages.

## CLI

All commands print stable, key-sorted JSON. Business errors use an `E_xxx` code and return a non-zero exit status.

```bash
python -m schema_migrator --workspace WORKSPACE load \
  --schemas samples/schemas.json \
  --migrations samples/migrations.json \
  --queries samples/queries.json \
  --tolerance 0.01

python -m schema_migrator --workspace WORKSPACE migrate --migration m001
python -m schema_migrator --workspace WORKSPACE migrate --all
python -m schema_migrator --workspace WORKSPACE migrate --to 3
python -m schema_migrator --workspace WORKSPACE resume
python -m schema_migrator --workspace WORKSPACE explain --template q_order_totals --migration m001
python -m schema_migrator --workspace WORKSPACE status
python -m schema_migrator --workspace WORKSPACE verify
```

`verify` exits with code `1` if catalog metadata and the actual SQLite schema disagree. `load` and `migrate` are idempotent for identical content.

## Deterministic Demo

```bash
python -m schema_migrator.demo --base .demo-runs
python -m schema_migrator.demo --base .demo-runs > run-1.json
python -m schema_migrator.demo --base .demo-runs > run-2.json
cmp run-1.json run-2.json       # identical on Linux/macOS
# Windows PowerShell: Compare-Object (Get-Content run-1.json) (Get-Content run-2.json)
```

The demo creates two independent workspaces. In each run it:

1. loads v1;
2. force-kills a fresh process after `alter_column_type` has committed to the hidden migration stage;
3. confirms the active version is still v1;
4. resumes and checks that committed operations are not replayed;
5. completes v2, v3, v4;
6. runs `status`, `explain`, and `verify`;
7. compares the two transcripts byte-for-byte.

The demo output includes `identical`, `transcript_digest`, final version, recovery result, and per-template decisions.

## Sample Scenario

`samples/schemas.json` contains four complete schema versions:

- v1: baseline commerce schema with real-valued order totals and legacy customer codes.
- v2: adds `customers.tier`, creates an index, replaces `v_order_totals`, converts `orders.total_amount` from `REAL` to `DECIMAL(12,2)`, and replaces a trigger.
- v3: converts `order_items.unit_price` to `DECIMAL(10,2)`, adds `discount_rate`, and replaces `v_item_summary`.
- v4: drops `customers.legacy_code`, `idx_customers_legacy`, and `idx_orders_status`.

`samples/migrations.json` contains `m001`..`m003`. `samples/queries.json` contains ten deterministic query templates, including:

- a deliberately unaffected count query;
- queries affected by a type conversion;
- a query that depends on a changed view;
- a query that depends on a retired column;
- query cache entries that explicitly depend on indexes.

Template decisions are one of:

- `REUSABLE`: no dependency changed; rows are verified identical across versions.
- `CACHE_INVALIDATED`: the rewrite cache/binding must be rebuilt, but the target query remains executable.
- `PRECISION_LOSS`: a type conversion can round or narrow values; the original/new type and first affected column are reported.
- `INCOMPATIBLE`: a referenced column was removed or the type change cannot preserve meaning.

## Atomicity & Recovery Model

Each committed version is an immutable SQLite snapshot:

```text
WORKSPACE/
  catalog.sqlite3
  snapshots/
    v0001-<content-digest>.sqlite
    v0002-<content-digest>.sqlite
  .writer.lock
```

The catalog stores:

- the singleton version pointer;
- version/object/content digests;
- migration history and full diagnostic reports;
- one unfinished-migration journal.

The pointer is replaced as a single table-shaped row set inside one SQLite transaction (`TEMP` pointer table, delete singleton, insert replacement). Readers consume the pointer in a read transaction and then open only the referenced immutable snapshot, so migration intermediates are never visible.

Migration execution uses a copied hidden stage:

1. copy the current snapshot to a stage;
2. persist the planned operation sequence, target schema, base pointer, and content digest in the catalog journal;
3. execute one operation per SQLite transaction;
4. write its old/new values, conversion scale, rounding mode, and operation order to `__schema_diag_log` in the same transaction;
5. persist applied operations in the journal;
6. verify foreign keys, integrity, schema shape, query results, and content digest;
7. move the complete stage to its final digest-named path;
8. atomically replace the catalog pointer and insert version/object/report metadata.

On recovery, operation rows in the stage log are authoritative. If a process died after a stage operation committed but before the catalog journal was updated, the operation is recognized as applied and is not executed again. If a transaction rolled back, the missing log row causes only that operation to be replayed.

## Concurrency

- A cross-platform advisory writer lock serializes migration writers.
- Catalog transactions use WAL mode with `busy_timeout` and `synchronous=FULL`.
- Readers do not take the writer lock.
- The pointer is compare-and-swapped against the migration base version and content digest.
- Two processes submitting the same migration produce one `INCREMENTAL_APPEND` and one `IDEMPOTENT` result.
- A conflicting pointer or version returns `E_CONCURRENT_MODIFICATION`, `E_VERSION_CONFLICT`, or `E_VERSION_ROLLBACK` rather than overwriting.

## Reproducibility

- No timestamps or random IDs are written to reports.
- JSON is emitted with sorted keys.
- Snapshot content digests cover canonical schema metadata and canonicalized table rows, not nondeterministic file bytes.
- Numeric conversion uses Python `Decimal` with an explicit scale and rounding mode (`ROUND_HALF_EVEN` by default).
- Every converted/assigned value records its operation order and source/target scale.
- Damage reports, object versions, and demo transcripts are deterministic.

## Error Codes

Representative business errors include:

```text
E_JSON E_SCHEMA E_NAME E_DUPLICATE_NAME E_TYPE E_DEFAULT
E_FOREIGN_KEY E_FOREIGN_KEY_TYPE E_INDEX E_VIEW E_TRIGGER
E_DEPENDENCY E_DEPENDENCY_MISSING E_DEPENDENCY_CYCLE
E_MIGRATION E_MIGRATION_OP E_MIGRATION_ORDER E_MIGRATION_TARGET_MISMATCH
E_MIGRATION_CONFLICT E_MIGRATION_NOT_FOUND E_VERSION E_VERSION_GAP
E_VERSION_CONFLICT E_VERSION_ROLLBACK E_CONCURRENT_MODIFICATION
E_JOURNAL_PENDING E_RECOVERY_STATE E_TARGET_SCHEMA_DRIFT
E_QUERY_EXECUTION E_RESULT_DRIFT E_HASH_MISMATCH E_LOCK_TIMEOUT
E_NOT_LOADED E_ALREADY_LOADED E_SNAPSHOT_MISSING
```

Static schema/migration validation occurs before the catalog or snapshot is written. Invalid input therefore returns the specific code while leaving the current version pointer and committed snapshots unchanged.

## Tests

```bash
python -m pytest
```

The suite covers:

- duplicate names, invalid defaults, foreign-key type mismatch, migration order, and target mismatch;
- dependency closure across columns, indexes, views, triggers, and query templates;
- precision-loss versus incompatible decisions;
- conversion scale/order traceability;
- interrupted migration at `add_column`, `replace_view`, and `alter_column_type`;
- two-process idempotent commit;
- SQLite schema tamper detection;
- byte-identical repeated demo output.

Actual local verification in this workspace:

```text
Python 3.12.13 (.venv)
SQLite 3.53.1
15 passed
demo transcript digest: 2a8cd6f9d842d0f2d2e1a356282583deb90aaa62dff3ab3be118fcb14fdde476
```

## Design Notes

- SQLite DDL is transactional, but SQLite cannot atomically expose a partially rebuilt table to readers. Immutable stage snapshots plus a pointer swap avoid that class of half-state.
- Rebuilding a type-changed table temporarily removes and recreates dependent views in dependency-topological order inside the hidden stage. The active snapshot is untouched.
- Version rollback is rejected by policy (`E_VERSION_ROLLBACK`). Re-submitting an already committed migration ID is treated as idempotent even when the pointer has advanced further; the result is marked `stale: true`.
- A migration declared with `mode: "FULL_REPLACE"` is classified as such in the report. Incremental migrations are classified as `INCREMENTAL_APPEND`.
- Templates are validated against at least one declared schema version. Their `available_from` version prevents a future-only template from being reported as affected by an earlier migration.
