# Local CDN Edge Cache Consistency Workbench

This repository implements a deterministic, local CDN edge cache consistency
and origin-arbitration workbench. It loads two cache generations, validates and
merges them, evaluates freshness from an injected logical clock, performs
conditional revalidation against a programmable loopback origin, records
injection-driven inconsistencies, chooses between exact and digest-bucket
sampling validation, evicts entries deterministically, and persists committed
state and audit evidence in SQLite.

The implementation uses only local deterministic samples and a loopback HTTP
server. It does not contact external sites or require an account.

## Environment

- Verified runtime: CPython 3.12.13.
- Source remains compatible with CPython 3.10.12.
- Required packages: `numpy`, `flask`, `pytest`.

```bash
python3 -m pip install -r requirements.txt
python3 scripts/generate_samples.py
```

The project can be used without installation by setting `PYTHONPATH=src`, or it
can be installed with `python3 -m pip install -e .`.

## Quick Start

Run one consistency round. The loopback origin starts on an ephemeral port and
is shut down automatically.

```bash
PYTHONPATH=src python3 -m cdn_edge_workbench run \
  --samples samples \
  --state state \
  --logical-clock 1200
```

Inspect only committed state:

```bash
PYTHONPATH=src python3 -m cdn_edge_workbench inspect --state state
```

Simulate an abrupt exit after a prepared result is journaled:

```bash
PYTHONPATH=src python3 -m cdn_edge_workbench run \
  --samples samples \
  --state state \
  --fault-point after_prepare \
  --run-id crash-demo
```

Recover prepared work and clean orphan tables:

```bash
PYTHONPATH=src python3 -m cdn_edge_workbench recover --state state
```

Run the automated suite:

```bash
PYTHONPATH=src python3 -m pytest -q
```

## Sample Layout

All sample-internal file references are relative paths.

```text
samples/
  origin_manifest.json       origin resources, bodies, versions, ETags, TTLs
  nodes.json                 node region, capacity, validation budget
  policy.json                logical clock, tolerances, retry and scale policy
  v1_snapshot.json           generation 1 single-file cache snapshot
  v2_meta.sqlite             generation 2 shard metadata and SHA-256 hashes
  v2/shards/edge-a.json      generation 2 shard for edge-a
  v2/shards/edge-b.json      generation 2 shard for edge-b
  v2/shards/edge-c.json      generation 2 shard for edge-c
  injections.json            parse/merge/freshness/origin anomalies
state/
  .gitkeep                   empty committed-state target
```

The checked-in samples demonstrate:

- cross-node and cross-generation version divergence;
- equally consistent candidates;
- v1 relative-seconds conversion with `ceil` rounding;
- fresh, boundary, and expired entries;
- parse-stage same-version tampering;
- merge-stage ETag conflict;
- freshness-stage clock rollback;
- stale 304, request timeout, malformed headers, and unchanged 200;
- valid 304 refresh and valid 200 full replacement;
- sampled and exact comparison modes in one run;
- deterministic capacity eviction.

## CLI

`run` options:

| Option | Meaning |
| --- | --- |
| `--samples DIR` | Sample root, default `samples`. |
| `--state DIR` | Persistent state root, default `state`. |
| `--logical-clock N` | Injected logical clock; defaults to `policy.json`. |
| `--run-id ID` | Stable idempotency key. Defaults to a deterministic sample/clock digest. |
| `--fault-point after_prepare` | Exit after prepared audit shadow rows are journaled. |
| `--fault-point during_apply` | Exit inside the final transaction after staging entries. |
| `--origin-url URL` | Use an already-running loopback origin instead of the embedded server. |
| `--fail-on-inconsistency` | Exit 3 when audit records exist. |

Exit codes:

- `0`: run committed.
- `2`: business/validation error.
- `3`: committed with audit findings and `--fail-on-inconsistency`.
- `70`: requested simulated crash; persistent recovery state may remain.

## Data Contracts

An origin resource contains `path`, `body`, `content_version`, `etag`,
`digest_algorithm`, optional `content_digest`, `cache_control.max_age`,
`cache_control.s_maxage`, `cacheable`, and `last_modified_clock`.

Supported ETag forms include:

```text
"sha256:<hex>"
"sha256-<hex>"
"<hex>"            with a declared digest algorithm
W/"sha256:<hex>"   weak marker retained in cache metadata
```

Every declared ETag digest must match the declared content digest and, for
origin resources, the body bytes.

A cache entry contains `node`, `path`, `content_digest`, `content_version`,
`etag`, `last_verified_clock`, `expires_at_clock`, `cache_generation`,
`source_version`, `source_ref`, `raw_time`, and `effective_ttl_seconds`.

v1 entries use `last_verified_age_seconds` and `ttl_remaining_seconds`.
Conversion records store the raw value, seconds-per-tick scale, rounding
direction, unrounded quotient, and converted ticks. v2 entries use absolute
logical clocks.

## Arbitration

Candidates for a path are sorted by this total order:

1. higher last-verified logical clock;
2. higher content version;
3. higher cache generation;
4. higher source-format priority (`v2` over `v1`);
5. stable node/path/digest/ETag/raw-time keys.

If all comparison fields are equal, the decision is `consistent`. Otherwise it
is classified as `version_divergence`, `digest_conflict`, `etag_conflict`, or
`metadata_divergence`. Each decision includes all sorted candidates, the
winner, structured basis, loser difference summaries, and a SHA-256 decision
hash that is invariant under candidate input order.

## Freshness

Freshness never reads the system wall clock.

```text
remaining = expires_at_clock - injected_logical_clock

remaining > tolerance                  -> fresh
-tolerance <= remaining <= tolerance   -> boundary
remaining < -tolerance                 -> expired
```

A global clock rollback larger than `clock_rollback_tolerance_ticks` returns
`E1101_CLOCK_ROLLBACK` before any index replacement. Per-path injected rollback
terminates only that path and is recorded in the audit trail.

## Conditional Origin Revalidation

Non-fresh entries send both `If-None-Match` and `If-Modified-Since` to the
loopback origin. The server uses ephemeral ports and is closed after the round.
It supports normal conditional behavior plus forced behaviors:

- `stale_304`
- `unchanged_200`
- `content_tamper_same_version`
- `request_timeout`
- `malformed_headers`
- `connection_refused`

A successful 200 requires both ETag and content version to change and increments
cache generation. A valid 304 changes only last-verified and expiry clocks.
Transport retries are bounded by `origin_retry_budget`; the result records
attempts and `retry_exhausted`.

## Scale Selection and Eviction

When a node entry count or candidate count exceeds `comparison_threshold`, the
node/path switches from `exact_per_entry` to deterministic digest-bucket
sampling. The output includes:

- actual mode and trigger condition;
- rejected mode and reason;
- sample fraction and seed;
- sampled digest buckets and checked item count;
- conservative false-negative bound
  `1 - sampled_digest_buckets / total_digest_buckets`;
- observation source, limited to loaded index/snapshot digest buckets and a
  deterministic SHA-256 bucket ranking.

Eviction first removes expired entries, then the oldest last-verified logical
clock, then stable node/path/generation/digest ordering. Every victim and every
retained entry reason is recorded.

## Persistence and Concurrency

Committed readers use one SQLite transaction to read:

- the index pointer and its versioned entry table;
- merged arbitration decisions;
- eviction and retention records;
- consistency audit rows;
- origin watermarks.

Prepared audit/arbitration/eviction rows use `committed=0` and are invisible to
committed readers. The final transaction creates a staging table, renames it to
`index_entries_v<N>`, advances the single index pointer, flips shadow rows to
committed, and updates watermarks. A version pointer never moves backward.

An exclusive `flock` serializes writers. A replacement ledger keyed by
`(node, path, new_cache_generation)` prevents two different results from
replacing the same cache generation. Stable `--run-id` values and consistency
receipts make repeated submissions idempotent.

Recovery drops orphan staging tables, detects uncommitted audit rows, replays
prepared results, and leaves plan-only runs in a `requires_replan` state.

## Business Error Codes

| Code | Meaning |
| --- | --- |
| `E1001_INVALID_JSON` | Unreadable or invalid JSON. |
| `E1002_SCHEMA_INVALID` | Invalid sample structure. |
| `E1003_PATH_INVALID` | Non-canonical or malformed path. |
| `E1004_PATH_TRAVERSAL` | Traversal or escaping path. |
| `E1005_VERSION_INVALID` | Non-positive content version. |
| `E1006_VERSION_NOT_MONOTONIC` | Content version regression. |
| `E1007_ETAG_INVALID` | Unsupported/malformed ETag. |
| `E1008_ETAG_DIGEST_MISMATCH` | ETag, digest, or body mismatch. |
| `E1009_CACHE_CONTROL_INVALID` | Invalid max-age/s-maxage. |
| `E1010_NODE_CONFIG_INVALID` | Invalid node config. |
| `E1011_INJECTION_INVALID` | Invalid or unreachable injection. |
| `E1012_INJECTION_REFERENCE_MISSING` | Unknown injection path/node. |
| `E1013_SHARD_HASH_MISMATCH` | v2 shard hash mismatch. |
| `E1014_SHARD_METADATA_INVALID` | Invalid v2 shard metadata. |
| `E1015_CACHE_GENERATION_ROLLBACK` | Cache generation regression. |
| `E1016_DUPLICATE_KEY` | Duplicate resource/cache key. |
| `E1017_POLICY_INVALID` | Invalid policy value. |
| `E1101_CLOCK_ROLLBACK` | Logical clock rollback exceeds tolerance. |
| `E1201_ORIGIN_STALE_304` | 304 despite source/cache entity mismatch. |
| `E1202_ORIGIN_UNCHANGED_200` | 200 with unchanged ETag and version. |
| `E1203_ORIGIN_PARTIAL_CHANGE` | Only one of ETag/version changed. |
| `E1204_ORIGIN_REQUEST_TIMEOUT` | Single-request budget exceeded. |
| `E1205_ORIGIN_MALFORMED_HEADERS` | Malformed HTTP response headers. |
| `E1206_ORIGIN_CONNECTION_REFUSED` | Connection refused/closed without response. |
| `E1207_ORIGIN_RETRY_EXHAUSTED` | Retry budget exhausted (also carried as a detail). |
| `E1208_ORIGIN_BAD_RESPONSE` | Other invalid origin response. |
| `E1209_ORIGIN_UNCACHEABLE` | Reserved for uncacheable origin responses. |
| `E1301_INJECTED_CONTENT_TAMPER` | Same-version content tampering. |
| `E1302_INJECTED_ETAG_CONFLICT` | Injected ETag conflict. |
| `E1303_INJECTED_CLOCK_ROLLBACK` | Injected per-path clock rollback. |
| `E1401_INDEX_VERSION_ROLLBACK` | Index version pointer regression. |
| `E1402_CONCURRENT_MODIFICATION` | Conflicting writer or lock timeout. |
| `E1403_RECOVERY_INCOMPLETE` | Prepared state cannot yet be applied. |
| `E1404_STATE_CORRUPT` | Dangling/inconsistent state pointer. |
| `E1405_RUN_ID_CONFLICT` | Same run id with different plan/result. |
| `E1406_INVALID_ARGUMENT` | Invalid CLI/API argument. |

## Further Documentation

- `docs/TECHNICAL.md`: architecture, transaction sequence, and algorithms.
- `docs/VERIFICATION.md`: commands and actual observed verification results.
- `samples/README.md`: deterministic sample inventory.
