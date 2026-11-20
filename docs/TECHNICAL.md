# Technical design

## 1. Data flow

```text
package manifest
  -> source SHA validation
  -> v1 validation + v2 manifest/shard/metadata validation
  -> cross-version merge and arbitration
  -> in-memory GraphIndex (ID map, sorted adjacency, label-prefix index)
  -> version-staged persistent index
  -> atomic current-index pointer switch
  -> deterministic BFS traversal
  -> checkpointed batch commit and watermark
```

Package parsing and full validation happen before `GraphStore.replace` performs any persistent write. This is why malformed packages cannot leave a partially loaded index or advanced clock.

## 2. Validation

### v1

The loader rejects duplicate JSON keys and non-finite JSON constants. It then validates:

- unique, non-empty node ID;
- node label type;
- finite node weight;
- non-negative integer `relative_write_seconds`, excluding booleans;
- even-length flat `adjacency`;
- exact order/multiplicity equality between `adjacency` pairs and `out_edges`;
- strictly sorted, duplicate-free `linear_index` covering exactly the node set.

### v2

The v2 manifest, metadata DB and shards are validated together:

- metadata values match the manifest;
- shard order, generation monotonicity and global generation are valid;
- canonical shard content hash matches the shard file, manifest and metadata;
- canonical IDs are globally unique;
- edge target/label are non-empty strings and edge weight is finite and non-negative;
- node generation equals shard generation;
- `state` matches `tombstone`;
- per-shard counts and adjacency digests match metadata;
- the global v2 adjacency digest matches metadata and manifest.

The package layer additionally verifies every declared SHA-256, merged node bounds and the combined merged adjacency digest.

## 3. Merge and arbitration

Each candidate node is ranked by:

```text
(source logical clock, shard generation, source format priority)
```

`v2` has priority `20` and `v1` has priority `10`, so an otherwise exact tie resolves to v2. Every candidate and the winning reason are persisted. A v1 node is migrated with:

- `generation = 0`;
- `tombstone = false`;
- absolute timestamp derived from the injected clock anchor and declared rational scale;
- a migration decision containing anchor, scale numerator/denominator, rounding direction and absolute timestamp.

v2 absolute timestamps are preserved unchanged.

Edges are keyed by `(from_id, to_id, label)`. All source candidates remain attached to the chosen edge through `sources_json`. The winning edge uses the same tiered source ranking; equal-source duplicates use a deterministic weight tie-break. If the winning node is a tombstone, its outgoing edges are suppressed rather than inherited from an older visible copy of that node.

The in-memory index contains:

- `dict[node_id] -> UnifiedNode` for exact lookup;
- sorted adjacency tuples per source node;
- sorted labels plus `label -> sorted node IDs` for prefix lookup via `bisect_left`.

No index build or lookup scans all nodes to answer exact/prefix queries.

## 4. Traversal determinism

Supported normalized query forms:

```json
{"kind":"exact","node_id":"n1","audit":true}
{"kind":"label_prefix","prefix":"per","include_tombstones":false}
{"kind":"reachable","node_id":"n1","depth":2}
{"kind":"filtered","node_id":"n1","depth":3,"edge_labels":["follows"],"min_weight":0.5}
```

Rules:

- BFS processes complete depth layers in order.
- Neighbors are sorted by `(to_id, edge_label, weight, source format, source version)`.
- A node is emitted at most once per traversal; the first BFS path wins.
- Edge labels and weights filter individual edges.
- Edge weights are never summed, so floating-point addition order cannot affect results.
- Final results sort by `(depth, node_id, path, edge-label chain)`.
- Truncations sort by `(path length, path, reason, edge-label chain)`.
- Exact and audit-prefix queries retain tombstones. Normal reachability suppresses tombstone nodes and records a truncation whose path ends at the tombstone.
- JSON is emitted as canonical UTF-8 with sorted keys and no insignificant whitespace.

## 5. Persistent schema

The SQLite store runs in WAL mode with full synchronous commits:

- `clock_state`: injected anchor, monotonically advanced logical clock.
- `index_versions`: package identity, source/applied clock, classification, build phase/cursor, counts, digest and audit JSON.
- `current_index`: singleton atomic pointer to the committed index version.
- `index_nodes`, `index_edges`: complete version-scoped adjacency/index rows.
- `merge_decisions`, `migration_decisions`: traceable node/edge arbitration and v1 migration records.
- `traverse_batches`: query, batch-content digest, status, next depth, frontier/results/truncation checkpoint and final result digest.
- `traverse_watermarks`: one committed watermark per batch.

No table is updated in place across index versions. A replacement builds new version-scoped rows and switches only `current_index` in the final transaction.

## 6. Replacement protocol

1. Validate package outside the store.
2. Acquire the cross-process replacement file lock.
3. Compare current and incoming package version, logical clock, manifest/source/shard digests, node bounds and adjacency digest.
4. Classify as `no_op`, `full_replace`, or `incremental_append`.
5. On rejection, leave the current pointer and staged rows untouched.
6. Create a `building` version row.
7. Stage nodes in sorted ID batches, checkpointing the last ID in the same transaction.
8. Stage edges in sorted key batches, checkpointing the last edge key in the same transaction.
9. Insert merge/migration decisions, verify staged counts and adjacency digest, mark the version committed and switch the pointer in one transaction.
10. On a killed build, the old current pointer remains readable. Repeating the same package resumes from `stage_phase`/`stage_cursor`; a now-stale build is rejected if a newer index was committed in the meantime.

An append package is classified using its manifest declaration, version, bounds and hashes, then materialized as a new complete version before the atomic pointer switch. Readers therefore never observe a half-appended adjacency table.

Version rollback and clock rollback are rejected by default. `--downgrade-policy allow` records an explicit downgrade reason and builds a new complete version.

## 7. Crash recovery

Traversal progress is checkpointed:

- `prepared`: batch identity/query committed before traversal starts.
- `running`: after each completed BFS layer, or after exact/prefix selection, `frontier`, `visited`, partial results and truncations are committed together with `next_depth`.
- `committed`: result JSON, its SHA-256, stats and watermark are committed in one transaction.

A hard kill can only roll back the current uncommitted checkpoint. Resume starts at the last committed `next_depth`; completed layers are not replayed. Stats are derived from the checkpointed result set, and the watermark is inserted exactly once, so recovery cannot double-count emissions, visits or truncations.

The acceptance test uses real child-process `SIGKILL` (`os.kill(pid, 9)`) after the first BFS layer checkpoint and compares the resumed canonical result to an uninterrupted run field by field.

## 8. Concurrency and idempotency

- SQLite `BEGIN IMMEDIATE`, unique keys and WAL isolate readers from uncommitted writes.
- A replacement file lock serializes index-pointer transitions.
- A per-batch file lock serializes writers for the same batch; different batches can progress concurrently.
- `UNIQUE(package_id, package_version)` prevents conflicting commits of the same package version.
- `traverse_batches.batch_id` and `traverse_watermarks.batch_id` are primary keys, so a batch commits once.
- Repeating an identical replacement returns `no_op`; repeating a committed batch returns its stored result.
- Readers resolve the current pointer and then read version-scoped rows, so they only observe complete committed versions.

## 9. Clock and time

No application code calls `time`, `datetime`, `time_ns`, `monotonic` or a system-clock API. The only time source is `samples/clock.json`:

```text
anchor_ms = 1710000000000
step_ms   = 250
max_rollback_ms = 1000
v1 conversion = 1000 ms/second, floor
```

The anchor is persisted and the current logical clock advances by one injected step on committed state transitions. Clock configuration changes that would move the anchor backwards beyond the configured threshold are rejected.

## 10. Verification surface

`status` reports:

- current index version, package version, node/edge/tombstone counts;
- current adjacency digest;
- logical clock;
- pending build stage;
- committed traversal batch count and latest committed clock/batch ID.

`verify` recomputes node/edge/tombstone counts and adjacency digest, validates merge/migration decision counts, validates every batch's query/content/result/stats digests and validates each committed watermark against its batch. It also reports pending builds without treating them as visible indexes.

