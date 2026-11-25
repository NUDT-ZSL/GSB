# Technical Design

## 1. Module Layout

```text
modelwb/
  domain.py         manifest dataclasses, shard/layer validation, digest chains
  timeconv.py       Decimal-based v1 relative -> absolute millisecond conversion
  loaders.py        v1 JSON / v2 shard JSON + SQLite loading, merge, references
  patching.py       patch parsing, incremental application, divergence diagnostics
  invalidation.py   dependency graph and cache invalidation closure
  storage.py        SQLite schema, file lock, idempotent operation/step records
  workbench.py      orchestration, gray/rollback FSM, request binding, recovery
  api.py            Flask API
  seed.py           deterministic offline sample generator
  cli.py            load/patch/gray/rollback/resume/status/verify commands
tests/test_workbench.py
```

## 2. v1 Input

`samples/v1_manifest.json` is the executable schema example.

```json
{
  "format": "v1",
  "time": {
    "base_ms": 1700000000000,
    "scale_ms": "1000",
    "rounding": "half_up"
  },
  "registries": {
    "configs": {},
    "feature_tables": {},
    "upstream_models": {}
  },
  "versions": [
    {
      "version": "classifier-v1",
      "revision": 2,
      "logical_clock": 100,
      "generation": 1,
      "updated_relative": "1.5005",
      "preprocess": "prep-v1",
      "postprocess": "post-v1",
      "dependencies": {
        "feature_tables": ["features-v1"],
        "upstream_models": ["base-model-v1"]
      },
      "layers": [
        {
          "name": "embedding",
          "parameters": 4096,
          "shard_count": 1,
          "shards": [
            {
              "shard_id": "embedding-000",
              "index": 0,
              "shard_count": 1,
              "sha256": "<64 hex>",
              "size": 4096,
              "path": "shards/classifier-v1/embedding/embedding-000.bin"
            }
          ]
        }
      ]
    }
  ]
}
```

Validation rules:

- A version id is unique inside its source.
- `revision`/`sequence` is strictly increasing in source order and update times are
  nondecreasing.
- Layer names are unique; parameters are positive integers.
- Each non-tombstone layer has shards at indexes `0..n-1`; `shard_count`,
  `index`, and digest syntax must agree.
- Every preprocess/postprocess reference resolves to a config; config references
  are recursively resolvable and acyclic.
- Every feature table and upstream model reference resolves. An upstream may be a
  registry id or an actual model version; tombstoned model references are rejected.

## 3. Time Interoperability

v1 relative time is converted as:

```text
exact_ms = Decimal(base_ms) + Decimal(updated_relative) * Decimal(scale_ms)
absolute_ms = exact_ms.quantize(1, rounding)
```

Supported modes are `floor`, `ceil`, `half_up`, and `half_even`. JSON values are
read with `parse_float=Decimal`, so no binary floating point enters the path.

The provenance record contains:

```json
{
  "kind": "relative",
  "base_ms": 1700000000000,
  "relative": "1.5005",
  "scale_ms": "1000",
  "rounding": "half_up",
  "exact_ms": "1700000001500.5",
  "absolute_ms": 1700000001501,
  "rounding_delta_ms": "-0.5",
  "rounding_direction": "up"
}
```

The unified view also exposes `time_interop.normalized_json`, a canonical
byte representation of the absolute millisecond value. The seed intentionally
contains `shared-model-v1` in both formats with the same absolute time; their
normalized representations are byte-identical.

## 4. v2 Input and SQLite Metadata

Version payloads live in `v2/versions/<version>.json`. The loader resolves each
file through `v2/meta.db` and verifies the declared file SHA-256.

Required metadata tables:

```sql
versions(
  version, canonical_id, revision, absolute_updated_ms, logical_clock,
  generation, patch_digest, preprocess, postprocess, shard_file,
  shard_json_sha256, tombstone
)
dependencies(version, kind, ref)
configs(id, kind, definition_json)
feature_tables(id, definition_json)
upstream_models(id, definition_json)
shards(version, layer, shard_id, shard_index, shard_count, sha256, size, path)
```

The SQLite row is authoritative for absolute time, generation, logical clock,
patch digest, pre/post references, dependencies, and tombstone state. If the shard
JSON also carries pre/post or dependencies, both copies must agree. Tombstones keep
version/time metadata but no layers and are excluded from active deployment.

## 5. Unified View and Merge

Entries are grouped by version. The winner is selected by:

```text
(logical_clock, generation, source_priority, absolute_updated_ms,
 revision, canonical_id)
```

`source_priority` is `v2=20`, `v1=10`; therefore equal logical clock and generation
resolve to v2. Every view entry retains all source candidates, raw time forms,
conversion provenance, shard chain, directory digest, config binding, dependencies,
and tombstone state.

## 6. Shard Directory Digest

For each version:

```text
layer_identity = {
  name, parameters,
  shards sorted by index: {
    layer, shard_id, index, shard_count, sha256, size,
    byte_start, byte_end
  }
}
directory_payload = {version, layers sorted by layer name}
directory_digest = SHA-256(canonical JSON bytes(directory_payload))
```

Canonical JSON uses sorted keys, compact separators, UTF-8, and no NaN/Infinity.
Paths and source locations are deliberately excluded so equivalent shard sets have
the same digest.

## 7. Incremental Patch Algorithm

A patch declares:

```json
{
  "patch_id": "patch-classifier-v1-to-v2",
  "base_version": "classifier-v1",
  "target_version": "classifier-v2",
  "logical_clock": 110,
  "generation": 2,
  "preprocess": "prep-v2",
  "postprocess": "post-v2",
  "dependencies": {
    "feature_tables": ["features-v2"],
    "upstream_models": ["base-model-v1"]
  },
  "changes": [
    {
      "layer": "embedding",
      "shard_id": "embedding-001",
      "sha256": "<new whole-shard digest>",
      "byte_start": 0,
      "byte_end": 1024,
      "range_sha256": "<digest of changed range in full target bytes>"
    }
  ],
  "patch_sha256": "<canonical digest of the patch body>"
}
```

Application:

1. Clone every baseline layer/shard.
2. Replace only declared shard digests.
3. Require the full replacement to have the same layer and shard sets and layer
   parameters.
4. Detect any baseline/full difference not declared by the patch and fail with
   `PATCH_UNDECLARED_SHARD_CHANGE`.
5. Verify each declared digest equals the full replacement digest.
6. Optionally verify the changed range's bytes and `range_sha256`.
7. Recompute each layer chain and the directory digest; compare against the full
   replacement.
8. On mismatch, return the first divergence as
   `{layer, shard_id, field, values...}`.

The workbench records logical shard-chain snapshots. When shard paths are present,
actual files are also hashed; missing bytes map to `SHARD_MISSING`, and byte/digest
differences map to `SHARD_DIGEST_MISMATCH`.

## 8. Invalidation Closure

Graph nodes include:

```text
model:<version>
shard:<version>:<layer>:<shard_id>
config:<config_id>
feature:<feature_table_id>
upstream:<upstream_id>
```

Edges point from a model to its shards/configs/features/upstreams; configs point to
referenced configs/features/upstreams; feature/upstream registry definitions point
to their dependencies and, where applicable, actual model versions.

For a patch, roots include old/new model versions, changed shards, old/new configs,
features, and upstreams. The workbench computes both forward roots and reverse
dependents. A cache is invalidated only when its bound model node is in the closure.
Reusable cache rows are untouched: their `valid` flag, shard chain JSON, chain digest,
and config binding remain unchanged.

The seed demonstrates transitive invalidation: changing `classifier-v1` invalidates
`ranker-v1` because `ranker-v1` references `classifier-v1` as an upstream model,
while `aux-v1` caches remain reusable.

## 9. Gray and Request Binding

Only replicas whose `baseline_version` matches the release baseline are candidates.
For the seed these are `replica-a` and `replica-b`.

Per-stage process:

1. Verify target shard files and directory digest.
2. Verify the persisted target snapshot and directory digest.
3. Require all candidates to be `warmed` with the target binding.
4. Select `ceil(ratio * candidate_count)` sorted replica ids, at least one.
5. Atomically write stage ratio, decision basis, selected replicas, and release
   watermarks.

Request routing uses the first 8 hex digits of `SHA-256(request_id)` modulo 10000 and
compares against `ratio * 100`. The selected binding stores:

```json
{
  "version": "classifier-v2",
  "snapshot_id": "snap-...",
  "directory_digest": "...",
  "shard_chain": [],
  "preprocess": "prep-v2",
  "postprocess": "post-v2",
  "configs": {"prep-v2": {}, "tokenizer-v1": {}, "post-v2": {}},
  "config_binding": "...",
  "patch_chain": [],
  "absolute_updated_ms": 1700000002500,
  "logical_clock": 110,
  "generation": 2
}
```

The binding is inserted once and never updated by later stages or rollback.

## 10. Warmup and Rollback FSM

```text
pending -> loading -> warmed -> gray
                           \-> rollback -> warmed(baseline)
```

Prewarm digest failure marks the replica `rollback`, marks the release `failed`, and
returns `SHARD_DIGEST_MISMATCH`; no gray stage is committed.

Rollback captures trigger source, reason, current stage/ratio, and entered gray
replicas. It derives a descending ratio plan and persists that plan before executing
steps. A crash after a committed step therefore resumes the same plan rather than
recomputing from a lowered current ratio:

```text
100 -> 75 -> 25 -> 0
```

The final step moves all candidate replicas to baseline bindings and atomically
replaces the current snapshot pointer with the baseline snapshot. In-flight request
bindings are not modified.

## 11. Persistence and Recovery

SQLite runs in WAL mode with `synchronous=FULL`, foreign keys, a 30-second busy
timeout, and `BEGIN IMMEDIATE` for write transactions. An additional `flock` on
`<state.db>.lock` serializes complex multi-step operations across processes.

Tables:

- `wt_operations` and `wt_operation_steps`: idempotency key, kind, status, phase,
  canonical request digest, result/error, durable step diagnostics.
- `wt_catalog_versions`, `wt_registries`: current merged catalog input.
- `wt_snapshots`: immutable, content-addressed snapshot payloads.
- `wt_replicas`, `wt_caches`: stage/version/config/shard-chain bindings.
- `wt_release_runs`, `wt_gray_stages`, `wt_rollback_steps`: release decisions.
- `wt_invalidation_events`, `wt_health_checks`: diagnostics and health evidence.
- `wt_requests`: immutable in-flight bindings.
- `wt_meta`: current/baseline snapshot, versions, update-round watermark, logical
  clock watermark, catalog digest.

Atomic boundaries:

- Patch: snapshot insert + cache invalidation + invalidation event + release creation
  + current-snapshot pointer + operation completion in one transaction.
- Gray stage: selected replica transitions + stage row + release watermarks + step in
  one transaction. Each stage is intentionally a separate durable commit.
- Rollback step: replica transitions + rollback row + release state + step in one
  transaction; the final step also atomically restores the baseline pointer and
  completes the operation.

Crash injection:

```text
after_prepare
load_before_commit
patch_before_commit
patch_after_commit
gray_after_warmup
gray_after_stage:<1-based index>
gray_after_ratio:<ratio>
rollback_after_step:<1-based index>
rollback_after_ratio:<ratio>
```

On restart:

- A prepared/running patch is compared again in memory. The deterministic snapshot
  id and `INSERT OR IGNORE` make the missing commit idempotent; no duplicate snapshot
  or invalidation event is created.
- A gray operation skips `wt_gray_stages` rows already committed and continues from
  the next missing ratio.
- A rollback operation uses the persisted descending plan and skips committed
  `wt_rollback_steps`.
- A committed operation returns its original stored result.

## 12. Deterministic Seed

`modelwb.seed` uses SHA-256-derived bytes only. No network, random source, real model,
or wall-clock value enters sample content. Generated files:

```text
samples/v1_manifest.json
samples/registries.json
samples/replicas.json
samples/v2/meta.db
samples/v2/versions/*.json
samples/shards/**/*.bin
samples/patch_v1_to_v2.json
samples/full_replacement/classifier-v2.json
samples/MANIFEST.json
```

`samples/MANIFEST.json` contains SHA-256 for every generated file. The automated test
generates this tree twice and compares both the manifest and logical v2 database
rows.

## 13. Actual Verification

Environment observed:

```text
Python 3.10.12
Flask 3.0.3
SQLite 3.37.2
Linux/WSL Ubuntu
```

Executed:

```bash
python3 -m unittest discover -v
```

Result:

```text
Ran 17 tests in 3.847s
OK
```

Recovery tests used real subprocesses with `MODELWB_CRASH_AT`; each interrupted
process exited with code `86`, then the CLI `resume` command produced the same target
directory digest, gray stage set, and rollback plan as the uninterrupted path.

Two independent seed generations produced byte-identical `MANIFEST.json` files:

```text
ed354e9028c6da21a395a39b756fcd17a971d1a04e59a8cd0f5a6405624b0921
```
