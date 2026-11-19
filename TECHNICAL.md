# Technical Design

## 1. Layering

```text
CLI (cli.py)
  -> Engine (engine.py): validation orchestration, replay, views, visibility, reads, GC
       -> Store (store.py): candidate generations, snapshot pointer, SQLite persistence
       -> Validation (validation.py): closed enums, values, clocks, ranges
```

`Engine` never reads the system clock. All event times, view clocks, query clocks, and GC clocks are injected integers. Wall-clock calls exist only for SQLite contention backoff and for test assertions.

## 2. Persisted state

Every published state generation contains:

- `snap_meta`: schema version, table/config/session snapshots, last logical clock, next version id, GC metadata.
- `snap_txns`: transaction id, start clock, isolation, status, actual commit clock, planned commit/outcome, ordering.
- `snap_versions`: version id, row key, row version number, create/delete transaction, create/update/delete clocks, canonical row data, SHA-256 content digest, lifecycle state, predecessor, merge count, and a persisted transaction lineage used as the stable visibility-basis chain.
- `snap_views`: deterministic view id, transaction, isolation, view clock, snapshot watermark, sorted active set, pinned generation, retention status, view digest.
- `snap_applied_ops`: idempotency key, fingerprint, persisted result, applied clock.
- `snap_gc_runs` / `snap_gc_retained`: collection totals and per-version retention reasons/view ids.

SQLite uses WAL, foreign keys, `synchronous=FULL`, and a bounded busy timeout.

## 3. Candidate generation and pointer swap

A mutating command:

1. Acquires a SQLite write transaction and reads the current `snapshot_head`.
2. Applies the semantic mutation to an in-memory state copy.
3. Writes a complete new generation with `status='staging'`, including all state tables and a canonical state digest.
4. Commits the candidate generation. It is not reachable from `snapshot_head`.
5. In a second short transaction, checks that the head base generation is unchanged, changes the candidate to `ready`, and compare-and-swap updates `snapshot_head`.

If a process dies after step 4 but before step 5, the head still points to the old complete state. `resume` identifies the staging generation, counts its half-written versions/transactions, deletes it, and replays missing logical steps. If the process dies after step 5, the operation is already durable and the applied-op key makes replay a no-op.

Concurrent writers may stage different generations. Only one compare-and-swap succeeds; others discard their candidate and retry against the new head. A candidate generation is the concrete staging-table equivalent required by the task.

## 4. Logical clock

For requested clock `R`, persisted last clock `L`, and tolerance `tol`:

- `R < L - tol` -> `CLOCK_REGRESSION`.
- Otherwise the effective clock is `max(R, L)`.

Views are reconstructed at logical time `T` using a half-open completion rule:

- A transaction is in the active set when `start_clock <= T` and its end clock is absent or `>= T`.
- The snapshot watermark is the maximum commit clock strictly `< T`.
- A commit at exactly `T` is therefore still active/not visible at `T`; it becomes visible at a later clock.

This tie rule removes physical execution order from same-clock views. The active set is always sorted by transaction id.

## 5. Visibility

For a view `V` and version `v`:

1. `v.state == rolled_back` -> invisible.
2. Creation by the viewing transaction: visible if that transaction is active or committed.
3. Creation by another transaction: visible only if committed, `commit_clock < view_clock`, and the creator is not in `V.active_txns`.
4. Deletion by the viewing transaction: visible to that transaction unless it rolled back.
5. Deletion by another transaction: effective only if committed and `commit_clock < view_clock`.

The visible row is the highest version number satisfying both creation and deletion rules. This naturally handles:

- delete then rebuild;
- a delete committed after an old snapshot;
- active/rolled-back deletes;
- rolled-back insert/update versions;
- reinsertion in the same transaction.

### Write conflict detection

Before update/delete, the visible target must also be the latest non-rolled-back version for that row. Otherwise a newer writer changed the row after this stream's snapshot and `WRITE_CONFLICT` is returned. Inserts likewise reject a newer invisible live version. This is first-updater-wins and prevents lost updates.

### Same-transaction merging

If a transaction updates a row version it created, the update mutates that same version's data/digest/update clock and increments `merge_count`. Insert followed by several updates in one transaction therefore stores one version, not a chain.

## 6. Read views and query consistency

- Start views are registered from a deterministic start event.
- `repeatable_read` reuses the immutable view clock/active set from transaction start.
- `read_committed` uses the effective operation/query clock and can register a new view.
- View id is `rv:<txn>:<isolation>:<clock>`, so same-clock reconstruction returns the persisted fields unchanged.
- Queries read one published generation selected at query start. SQLite's read transaction and generation rows guarantee no partial state.

Although a view stores `pinned_generation`, query evaluation reads the current published generation. This is required so a repeatable-read transaction can see its own later writes. GC preserves every version visible to every retained view, so the current generation still contains all data needed by the frozen other-transaction snapshot.

## 7. Range reads

A query:

1. Ensures/loads the read view.
2. Loads one consistent published generation.
3. Verifies every scanned version's content digest.
4. Finds the visible version per row.
5. Applies `where`, stable requested ordering, primary-key tie-breakers, and limit.
6. Emits row key, values, version metadata, visibility basis, transaction chain, view clock, snapshot watermark, active transactions, and latency.

Ordering uses column types (`integer`, `real`, text, boolean) rather than JSON string order. No dict/set iteration order affects output.

Query logical cost is:

```text
base + scanned_versions * version_cost + returned_rows * row_cost
```

If it exceeds `query_latency_budget_ms`, `QUERY_LATENCY_BUDGET_EXCEEDED` is returned.

## 8. GC safety

GC is a normal candidate-generation mutation. A version is retained if any of the following holds:

- a retained read view sees it;
- it is the visible version of a fresh snapshot at the GC clock (protects commit-tie semantics);
- it is the current live row head;
- it was created/deleted by an active transaction;
- its creator/deleter is not final;
- its create or delete transaction id is not below `gc_watermark_txn`.

Only versions below the watermark and without any retention reason are removed in the new generation. Successor predecessor pointers are relinked or terminated to avoid dangling references. Each retained version carries its transaction lineage, so physical history compaction does not change range-read basis chains.

Old generations pinned by retained views are not physically deleted. After a GC publishes a new generation, unpinned old ready generations are cleaned. Therefore:

- a concurrent query has one complete generation;
- a retained old view can still resolve its needed versions;
- an expired view returns `STALE_SNAPSHOT`;
- physical GC work cannot expose a broken chain.

`status.gc.last` exposes collection totals, per-reason counts, and the actual retained version/view reason records.

## 9. Async GC

`gc --async` launches the same engine as a detached process. The worker writes a normal candidate generation and updates GC run accounting. `status` is the completion/observation interface. No shared in-memory lock or wall clock affects semantic results.

## 10. Idempotency and recovery

Applied operation keys:

- `<txn>:start`
- `<txn>:op:<seq>`
- `<txn>:finish`

Each key stores a SHA-256 fingerprint and canonical result. Reapplying the same key/content returns the persisted result without producing another generation. Reusing a key with different content returns `IDEMPOTENCY_CONFLICT`.

`resume`:

1. Cleans unpublished staging generations.
2. Reports uncommitted transactions and half-written counts.
3. Replays the deterministic event timeline.
4. Skips every applied key and retries only missing steps.
5. Repeats safely with no duplicate commits or versions.

Concurrent finishers serialize on SQLite writer locks. One commits; the rest reload, observe the same applied finish key, and return the same persisted result.

## 11. Determinism verification

`verify --runs N`:

- loads table/config/sessions from the current snapshot;
- creates N fresh temporary databases;
- replays the same session timeline and optional GC clock;
- constructs a semantic artifact of transaction statuses, version data/digests, range results, and GC totals;
- compares canonical JSON and NumPy object arrays of version digests.

It also checks snapshot/version/view digests and predecessor integrity on the current database.

