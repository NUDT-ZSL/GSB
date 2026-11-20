# Verification Record

## Environment

```text
Python 3.12.13 (isolated .venv)
SQLite 3.53.1
numpy 2.5.5 available in the prepared environment
pytest 8.3.3
OS: local Linux workspace (Windows code paths are implemented via msvcrt/fcntl branches)
Network: not used
```

The implementation is compatible with Python 3.10+ and uses only the standard library at runtime. The prepared virtual environment uses Python 3.12.13 with SQLite 3.53.1.

## Automated Tests

Command:

```bash
python -m pytest -q
```

Observed result:

```text
...............                                                          [100%]
15 passed
```

Coverage includes:

- pre-materialization rejection of duplicate columns, invalid defaults, incomparable foreign keys, migration order errors, and target-schema mismatch;
- migration closure and traceability for columns, indexes, views, triggers, and templates;
- `REUSABLE`, `CACHE_INVALIDATED`, `PRECISION_LOSS`, and `INCOMPATIBLE` decisions;
- fixed-scale conversion logs and first affected column type reporting;
- hard process termination at `add_column`, `replace_view`, and `alter_column_type`;
- recovery without replaying an already committed operation;
- two concurrent migration processes producing one commit and one idempotent result;
- SQLite schema tamper detection by `verify`;
- byte-identical output from repeated deterministic demos.

## Forced Termination Matrix

Each case used `SIGKILL`-equivalent (`os._exit(91)`) after the named operation had committed to the hidden stage, then ran `resume`:

| Failpoint | Active version after kill | Operations replayed by resume | Verify |
|---|---:|---|---|
| `add_column` | 1 | `m001.002` .. `m001.006` | pass |
| `replace_view` | 1 | `m001.004` .. `m001.006` | pass |
| `alter_column_type` | 1 | `m001.005` .. `m001.006` | pass |

The committed operation itself (`m001.001`, `m001.003`, or `m001.004`) was not replayed. In all cases the type-conversion log contained exactly four `orders.total_amount` values, not eight, confirming no duplicate data conversion.

## Concurrency Observation

Two simultaneous `migrate --migration m001` processes:

```text
process A: INCREMENTAL_APPEND, current_version=2
process B: IDEMPOTENT, current_version=2
verify: true
```

Only one version/object/report row was committed.

## Deterministic Demo

Commands:

```bash
python -m schema_migrator.demo --base .demo-runs-final > run-1.json
python -m schema_migrator.demo --base .demo-runs-final > run-2.json
cmp run-1.json run-2.json
```

Observed:

```text
cmp exit status: 0
transcript digest: 2a8cd6f9d842d0f2d2e1a356282583deb90aaa62dff3ab3be118fcb14fdde476
run-1/run-2 digest (run_a): 2a8cd6f9d842d0f2d2e1a356282583deb90aaa62dff3ab3be118fcb14fdde476
final version: 4
recovery executed: true
verify: true
```

The demo uses two separate workspaces (`run_a`, `run_b`) in each invocation and contains an actual subprocess hard kill followed by `resume`.

The same verbose transcript was also compared across Python 3.10.12/SQLite 3.37.2 and Python 3.12.13/SQLite 3.53.1; the normalized transcript digest was identical.

## Full CLI Chain

```bash
python -m schema_migrator --workspace WS load
python -m schema_migrator --workspace WS migrate --all
python -m schema_migrator --workspace WS explain --template q_legacy_codes
python -m schema_migrator --workspace WS status
python -m schema_migrator --workspace WS verify
```

Observed:

```text
load: current_version=1, INITIAL
migrate --all: current_version=4, m001/m002/m003 INCREMENTAL_APPEND
explain q_legacy_codes: INCOMPATIBLE, original type TEXT, new type null
status: current_version=4, object versions and invalidation counts present
verify: ok=true, sqlite_schema/object_versions/version_chain/migration_history all pass
```

Migration `m001` produced the expected precision-loss decisions for order-total templates, one cache invalidation for the newly created status-index dependency, and reusable unaffected templates such as `q_customer_count` and `q_item_revenue`.

Migration `m003` marked `q_legacy_codes` incompatible after `customers.legacy_code` removal and invalidated `q_status_lookup` when `idx_orders_status` was dropped.

