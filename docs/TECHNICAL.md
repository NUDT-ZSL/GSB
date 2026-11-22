# Technical Design

## Execution Pipeline

One run executes the following deterministic stages:

1. Parse and validate policy, origin resources, nodes, v1 snapshot, v2 metadata
   and shards, and injections. This stage is pure and writes no state.
2. Acquire the state process lock.
3. Reject excessive global logical-clock rollback against the committed
   watermark.
4. Recover prepared runs and orphan staging tables.
5. Load the current committed index; seed from merged snapshots only when no
   committed index exists.
6. Apply parse and merge injections in memory, terminating their target paths.
7. Arbitrate same-key v1/v2 candidates and cross-node path candidates.
8. Apply freshness-stage clock injections and classify every non-blocked entry.
9. Select stale/boundary entries under each node's per-round budget.
10. Start the embedded origin, apply origin injection behaviors, and perform
    conditional revalidation with retry limits.
11. Select exact or digest-bucket validation for each node/path.
12. Evict per node capacity and produce retention reasons.
13. Journal the prepared result, including audit shadow rows.
14. Atomically publish the versioned index and committed evidence.

## Snapshot Loading

v1 time conversion uses Decimal arithmetic:

```text
ticks(raw) = rounding(raw / seconds_per_tick)
last_verified_clock = base_clock - ticks(last_verified_age_seconds)
expires_at_clock = base_clock + ticks(ttl_remaining_seconds)
```

The complete raw input and conversion are retained under `raw_time`. v2 shard
hashes are SHA-256 over the raw shard file bytes read from disk. Metadata hash,
generation, index, and entry count are all checked.

Snapshot-level generation must increase. For equal node/path keys, v2 cache
generation and content version must be non-decreasing relative to v1.

## Arbitration Total Order

```text
(-last_verified_clock,
 -content_version,
 -cache_generation,
 -source_format_priority,
 node,
 path,
 content_digest,
 etag,
 canonical(raw_time))
```

The first candidate is the winner. Decision hashes are computed over sorted
canonical candidates and the structured basis, making the hash independent of
caller input order.

## Freshness Classification

The tolerance band covers both sides of the expiry instant. A boundary entry is
not classified as fresh, so it is eligible for conditional revalidation.
No wall-clock function participates in this decision.

## Origin Response State Machine

For 304:

- compare source manifest version/digest and response ETag with the cached
  entry;
- any mismatch is `E1201_ORIGIN_STALE_304`;
- otherwise update only last-verified and expiry clocks.

For 200:

- reject missing/malformed ETag or version headers;
- classify unchanged ETag and version as `E1202_ORIGIN_UNCHANGED_200`;
- classify same-version changed content as `E1301_INJECTED_CONTENT_TAMPER`;
- validate the ETag against body bytes;
- classify one-sided metadata change as `E1203_ORIGIN_PARTIAL_CHANGE`;
- when both changed, replace content metadata and increment cache generation.

The raw HTTP client enforces a strict status line, header token/value syntax,
Content-Length consistency, no transfer encoding, and the per-request budget.

## Sampling Bound

Sampling is at digest-bucket granularity, not item granularity. A deterministic
SHA-256 ranking of

```text
sha256(sample_seed + "|" + content_digest)
```

selects `ceil(total_buckets * sample_fraction)` buckets, with at least one bucket
when entries exist. If the buckets are treated as a uniform observation frame,
the conservative upper bound for missing a fixed inconsistent bucket is:

```text
1 - sampled_buckets / total_buckets
```

The observation source is explicitly the loaded index/snapshot digest bucket
distribution at the injected logical clock. No external or random source is
used.

## SQLite Two-Phase Publication

Schema objects:

- `index_pointer`: singleton current version, checksum, count.
- `index_versions`: committed version metadata.
- `index_entries_v<N>`: one complete entry table per index version.
- `run_journal`: planned/prepared/committed state and result checksum.
- `audit`, `arbitrations`, `evictions`, `retention`: evidence rows with a
  `committed` visibility flag.
- `replacement_ledger`: one successful replacement per node/path/new generation.
- `consistency_receipts`: idempotency receipts.
- `origin_watermarks`, `watermarks`: origin and logical-clock high-water marks.

Prepare transaction:

1. compare the prepared result checksum for run-id idempotency;
2. delete prior uncommitted rows for that run;
3. insert audit/arbitration/eviction/retention rows with `committed=0`;
4. store the result JSON and checksum in `run_journal`.

Apply transaction:

1. verify base version and next version;
2. verify logical-clock watermark tolerance;
3. create a unique staging table and insert all entries;
4. rename the staging table to `index_entries_v<N>`;
5. insert version metadata and advance `index_pointer`;
6. enforce replacement-ledger uniqueness and insert receipts;
7. update origin/global watermarks;
8. flip evidence rows to committed;
9. mark the journal committed.

A crash before step 4 leaves only prepared shadow evidence. A crash inside the
apply transaction rolls the whole transaction back. Recovery reads the prepared
result and re-executes the apply transaction.

## Concurrency

Writers hold an exclusive `flock` on `<state>/.workbench.lock` for the complete
load/compute/apply cycle. This prevents two processes from computing against the
same base and racing publication. SQLite still uses `BEGIN IMMEDIATE`,
`busy_timeout`, WAL, and `synchronous=FULL` to protect direct readers/writers.

Committed readers select only `committed=1` evidence and read the pointer and
table in one SQLite read transaction.

## Determinism Notes

- Freshness and arbitration use only injected logical clocks.
- Network timeouts use `time.monotonic`, not the wall clock.
- Default run ids are derived from sample metadata, candidates, injections, and
  logical clock.
- JSON reports are emitted with sorted keys.
- Sample generation is deterministic and does not modify source resources during
  injection.

