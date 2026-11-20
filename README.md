# Local Deterministic Index Maintenance

This workspace implements a self-contained B+ tree index maintenance component,
secondary-index rebuild adjudication, invalidation closure reporting, atomic
SQLite recovery, multi-process idempotence, and a deterministic CLI verifier.
It does not use a container, network service, credential, or external database.

The checked-in sample is `samples/fixed_sample.json`. All repositories, output,
logs, and validation artifacts stay below this workspace.

## Environment

- Python 3.10 or newer (developed and validated locally on CPython 3.10.12;
  the code uses no 3.11-only feature and is Windows-compatible).
- Standard library: `sqlite3`, `multiprocessing`, `argparse`, `decimal`.
- NumPy 1.24 or newer. It is used for controlled byte-array digest construction.

Windows setup:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/check_environment.py
python -c "import sqlite3, multiprocessing, numpy; print(sqlite3.sqlite_version, numpy.__version__)"
```

No installation step is required for local execution if NumPy is already
available. The CLI can be invoked directly with `python -m index_maintenance.cli`.

## Deterministic Validation

```powershell
python scripts/run_validation.py
python -m unittest -v tests.test_index_maintenance
```

`scripts/run_validation.py` performs two fresh end-to-end runs, compares the
`load`, `apply`, `status`, and `verify` outputs byte-for-byte, checks both
split-chain and secondary-maintenance forced terminations, checks concurrent
operation ranges, checks duplicate idempotent submission, runs the full unit
suite, and writes:

- `artifacts/validation_report.json`
- `artifacts/validation/run-a/`
- `artifacts/validation/run-b/`
- `artifacts/validation/crash-resume/`
- `artifacts/validation/secondary-termination/`
- `artifacts/validation/concurrent-ranges/`

## CLI

```powershell
python -m index_maintenance.cli --workspace . load --sample samples/fixed_sample.json
python -m index_maintenance.cli --workspace . apply --from-seq 1 --to-seq 15
python -m index_maintenance.cli --workspace . status
python -m index_maintenance.cli --workspace . verify
python -m index_maintenance.cli --workspace . rebuild --index idx_customer
python -m index_maintenance.cli --workspace . resume
```

Every successful command writes one canonical JSON object to stdout.
`--workspace` defaults to the current directory and stores state in
`.index_maint/index.sqlite3` plus a cross-platform `maintenance.lock`.

Useful maintenance/test switches:

```powershell
# Apply only a range. A range that depends on an earlier pending range waits.
python -m index_maintenance.cli --workspace . apply --from-seq 1 --to-seq 8

# Deliberately terminate after N staged page splits (exit code 97).
python -m index_maintenance.cli --workspace . apply --crash-after-split 3

# Deliberately terminate during secondary maintenance.
python -m index_maintenance.cli --workspace . apply --crash-after-secondary 1

# Reproduce unfinished attempts, discard staging, and replay the remaining log.
python -m index_maintenance.cli --workspace . resume
```

`verify --expected-ledger-version N` checks optimistic ledger expectations and
returns `E_LEDGER_ROLLBACK` when the durable ledger is older.

## Sample Model

The sample declares:

- Table `orders` with `id` primary key, typed columns, non-null constraints, and
  a composite non-null unique constraint on `(tenant, email)`.
- Four secondary indexes with ordered keys, uniqueness flags, covering columns,
  and dependent query templates.
- Fifteen strictly ordered insert/delete/update operations.
- Page capacity 4, split factor 0.5, rebuild thresholds of 2 pending items and
  1 touched page, fixed scale 3, `HALF_UP`, tolerance 0.001, and injected logical
  clock steps.

At operation 14, `idx_status_region` and `idx_region_order` touch more than the
configured page threshold and are rebuilt. `idx_customer` remains on the
incremental path. At operation 15, a note-only update leaves all index
structures reusable. The final status therefore retains both rebuild and
incremental decisions.

## Index Structure

The primary index and every secondary index have independent logical B+ trees.
Leaf entries are grouped by index key; each group contains one or more
`{pk, cover}` items. This handles duplicate keys in non-unique indexes and
updates covering values without changing grouping.

Each page stores:

- page ID, level, leaf/internal kind;
- parent pointer;
- left and right sibling pointers;
- grouped leaf entries or internal separator keys and child pointers;
- fixed-point entry/item count units and the conversion scale/rounding mode.

Insert overflow splits a leaf or internal page at a deterministic point derived
from `split_factor` while respecting both sides' fill bounds. Each created page
emits a split event containing its parent, left sibling, right sibling, level,
chain version, and trigger sequence. Internal root splits add one level.

Deletion below the fill lower bound first borrows from a same-parent sibling,
then merges deterministically. Internal underflow propagates upward and a
single-child root collapses. Retired page IDs remain in the tree provenance
list but are excluded from active page counts.

Tree snapshots include canonical page dictionaries, per-page SHA-256 digests,
a tree digest, cumulative split count, chain version, and allocator position.
`verify` checks:

- page and snapshot digests;
- reachability from the root and reciprocal parent/sibling pointers;
- level consistency, separator values, sorted keys, and fill bounds;
- operation replay, split-event replay, invalidation replay, row state, and
  watermark consistency.

## Rebuild Adjudication

For an affected secondary index, the engine computes:

- the exact old and new grouped items;
- pending logical item count;
- pre-change leaf pages touched by old/new keys;
- an incremental simulation and its expected split count;
- grouped-count fixed-point units and tolerance sensitivity.

If pending items or touched pages exceed the configured threshold, the index is
rebuilt from the post-operation row set. The decision records the first trigger,
the rejected incremental path, estimated incremental splits, actual rebuild
splits, logical clock start/window, and precision metadata. An index at or below
both thresholds uses incremental remove/replace/insert and is never rebuilt.

When a grouped count difference is smaller than the configured tolerance and a
reuse/rebuild conclusion depends on a threshold boundary, the decision is marked
`precision_sensitive` and includes its fixed-point error-source chain.

## Invalidation Closure

For each operation, the closure walks:

`changed column -> affected grouped index item -> page digest -> dependent query`

Committed invalidation records include trigger sequence, index, kind, action,
propagation path, reason, old/new reference, whether it was safely reused, and
resolution status. Unaffected indexes are not mutated; their snapshot and tree
digests remain byte-identical. Query status keeps both the latest action and the
latest recompute/rebuild sequence.

## Persistence and Recovery

Committed state lives in SQLite tables for metadata, normalized manifest,
snapshots, current rows, operations, split events, invalidations, ledger events,
and staged attempts.

One finalized operation is one SQLite transaction. It atomically stores the
operation result, incremental or rebuilt snapshots, row set, split events,
invalidation closure, ledger event, watermark, logical clock, and ledger version.
Unchanged snapshots are not rewritten.

During processing, split and secondary progress is written to a separate
`attempts` table. These staging rows are evidence only; they are never visible as
committed index state. A forced termination can leave:

- an `attempts` row in phase `started`, `split`, or `secondary`;
- an old committed snapshot/watermark;
- no exit report.

`resume` deletes staged attempts, then replays only operations absent from the
committed operation ledger. Because the unfinished operation was never in the
atomic snapshot transaction, its split counts and index items are counted once,
not twice. `verify` returns `E_UNFINISHED_ATTEMPT` until staging is resumed.

WAL mode, `BEGIN IMMEDIATE`, ledger-version rechecks, and an OS file lock ensure
that operation snapshots and split-chain versions are replaced as whole units.
Concurrent ranges that begin after an earlier pending range wait for it to
commit. Duplicate submissions of an already committed sequence with the same
content are no-ops; different content raises `E_OP_CONFLICT`.

## Error Codes

Errors are canonical JSON with `code`, `message`, and diagnostic `details`.

| Code | Meaning |
| --- | --- |
| `E_MANIFEST_MISSING` | Sample path does not exist |
| `E_JSON_MALFORMED` | Sample JSON is invalid |
| `E_BAD_SCHEMA_DECL` | Table/column/key/unique declaration is invalid |
| `E_BAD_INDEX_DECL` | Index/query declaration is invalid |
| `E_BAD_OP` | Operation structure, type, digest, or runtime precondition is invalid |
| `E_DUP_PK` | Insert reuses an existing primary key |
| `E_UNIQUE_VIOLATION` | Table or unique secondary constraint conflict |
| `E_CLOCK_ROLLBACK` | Injected logical clock moved backward beyond tolerance |
| `E_LEDGER_ROLLBACK` | Durable ledger is older than an event or expectation |
| `E_SPLIT_HEIGHT_OVERFLOW` | Split would exceed `max_tree_height` |
| `E_BROKEN_PARENT_POINTER` | Page parent/sibling/reachability chain is broken |
| `E_INDEX_DIGEST_MISMATCH` | Page, tree, snapshot, replay, or ledger digest mismatch |
| `E_UNFINISHED_ATTEMPT` | Verify requested while staged attempts are pending |
| `E_OP_CONFLICT` | Same operation sequence submitted with different content |
| `E_LOCK_TIMEOUT` | Workspace file lock was not acquired in time |
| `E_RANGE_WAIT_TIMEOUT` | Earlier operation range did not commit in time |
| `E_INCONSISTENT_STATE` | Stored state is structurally inconsistent |
| `E_NOT_INITIALIZED` | Workspace has no loaded manifest/state |
| `E_SAMPLE_MISMATCH` | Command sample differs from the loaded sample |
| `E_IO` | Local filesystem operation failed |

Static declaration/operation validation completes before the database is
created, so those errors leave no database. Runtime conflicts are recorded as
rejected operation-ledger entries and do not mutate index snapshots; later
operations continue.

## Recorded Validation

The latest local run recorded in `artifacts/validation_report.json`:

- 10 acceptance tests, 0 failures, 0 errors.
- Byte-identical `load`, `apply`, `status`, and `verify` outputs across two
  independent workspaces.
- Final state digest
  `db8e7797d6d996e744b71e78174f164f4ee4bd46d42d217626e0548e836bf7ea`.
- Split-chain forced termination at watermark 4, exit code 97, successful resume
  to the same state digest and zero remaining attempts.
- Secondary-maintenance forced termination at watermark 0, successful resume to
  the same state digest.
- Concurrent ranges `1..8` and `9..15`, concurrent duplicate full-range
  submissions, and all failure-code probes passed.

All generated verification outputs are under `artifacts/validation/`.
