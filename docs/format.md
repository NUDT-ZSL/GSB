# Format and Determinism Notes

This document records the concrete protocol choices used by the runnable
implementation. The fixture generator in `seqledger/sample.py` is the canonical
example.

## Topology

Each topic contains a name, a positive partition count, and a parseable
`regex` key rule. `sha256-mod` routing hashes the UTF-8 bytes of
`topic + U+001F + partition_key`, interprets the full SHA-256 digest as an
integer, and takes the remainder modulo the partition count.

## Event stream

`event_seq` is globally unique and strictly increasing in JSONL order. The
append transaction stores:

- deterministic partition and offset,
- partition key,
- payload digest,
- expected delivery generation,
- injected logical timestamp.

Offsets are assigned per partition as `0, 1, 2, ...`; a global sequence gap does
not create a partition-offset gap.

## v1 semantics

The v1 JSON document has a snapshot logical clock, relative effective seconds,
and a flat `delivery_alias`. For merge, the injected merge clock is the anchor:

```text
elapsed_ms      = relative_effective_seconds * ms_per_second
absolute_ms     = injected_merge_clock_ms + elapsed_ms
discarded_ms    = 0
```

The view records `scale_ms_per_second`, `rounding=floor`,
`discarded_remainder_ms=0`, original relative value, and snapshot clock.

## v2 semantics

`manifest.json` references shard files by relative path and records their
SHA-256 and generation. Generations must be non-decreasing in manifest order.
Each shard entry's generation must equal its shard generation. The SQLite
metadata database is opened read-only and must contain matching `semantics`
rows, closed `watermarks`, and closed `tombstones`. Every shard
`tombstone_ref` must resolve, and every tombstone row must be referenced by one
entry.

## Merge arbitration

Candidates are ordered by:

```text
(logical_clock_ms, semantic_generation, source_priority)
```

Source priority is `v2=2`, `v1=1`, and is the final tie-breaker. This makes the
logic-clock comparison primary and keeps all three dimensions auditable. Each
merged entry retains:

- winner source version and shard,
- original time representation,
- conversion or identity evidence,
- overridden candidate summaries.

## Replay

Delivery generation is the generation of the merged semantic entry. A delivery
is started only when:

- the event timestamp and semantic effective time are not in the future,
- the expected generation is satisfied,
- the upstream committed watermark satisfies the dependency,
- no tombstone is active,
- no claim for `(event_seq, delivery_generation)` exists.

The acknowledgment due time is:

```text
max(partition next_available_ms, event logical_ts_ms, semantic effective_ms)
  + ceil(1000 / max_rate_per_sec)
```

Each successful start and acknowledgment is committed separately. A partition
never has more than one in-flight record. The committed offset only advances on
acknowledgment or a terminal dead-letter decision.

## Claim idempotency

Delivery claims are keyed by `(event_seq, delivery_generation)`. A repeated
event is either an exact no-op or `EVENT_CONFLICT`. A repeated acknowledgment
finds the existing terminal claim and returns `idempotent=true`. A patch that
invalidates an in-flight claim records the claim as invalidated; the same
generation cannot be delivered twice.

## Verification

`verify` validates every immutable SQLite version first, then reconstructs the
expected state in memory from raw inputs. It considers both patch-before-replay
and patch-after-replay timelines and accepts a timeline only when the canonical
JSON projection is byte-for-byte equal. The projection includes topology,
events, subscriptions, semantic view, patch history, partition offsets, claims,
decisions, dead letters, invalidations, and conclusions.
