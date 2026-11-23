# Technical Design

## 1. Scope and Determinism

The workspace is a local, single-machine domain data editor and retrieval application. There are no network calls, credentials, GPUs, containers, or external datasets. Demo images are synthesized from integer seeds at request time.

Determinism relies on:

- fixed canvas shape and half-open pixel rectangles;
- explicit integer tile boundaries;
- `numpy.random.Generator(numpy.random.PCG64(seed))` for image generation;
- little-endian `float64` byte encoding for vectors;
- canonical JSON (`sort_keys`, compact separators, UTF-8, no NaN);
- BLAKE2b-derived deterministic term/projection vectors;
- canonical score strings formatted with `.17g`;
- natural pair-id ordering for ties.

`slide_rect` is `[x0, y0, x1, y1]`. Tile boundaries use:

```text
start + (end - start) * i // parts
```

so adjacent bounds share one explicit index and no floating-point rounding is involved.

## 2. Computation Graph

### Node `decode`

Inputs: `pair_id`, `image_seed`, `image_shape`, `slide_rect`, `texture_cell`, decoder algorithm version.

The seeded frame contains a deterministic gradient/noise background and a slide region made of four seeded rectangles with solid/checker/stripe/diagonal textures. The cache stores canonical metadata, the exact pixel bytes as base64, frame hash, and content hash.

### Node `visual`

Inputs: decode artifact hash, full feature parameter document, feature snapshot version/checksum.

For every tile:

- RGB mean (3 values, normalized to `[0, 1]`);
- per-channel histogram (`3 * hist_bins` values);
- deterministic signed projection to 16 dimensions;
- L2 normalization.

The artifact also stores tile bounds and the sorted block index. Equal alignment scores select the lowest block index.

### Node `text`

Inputs: text, locale, vocabulary snapshot version/checksum, tokenizer version.

Tokenization normalizes NFKC/casefold and scans left-to-right using longest vocabulary match. Unmatched spans return `E_VOCAB_MISSING` with `pair_id`, `field="text"`, token, and character offset. The embedding is the frequency-weighted sum of BLAKE2b-derived 16-dimensional term vectors, L2 normalized.

### Node `align`

Inputs: visual artifact hash, text artifact hash, similarity metric declaration, feature/vocab snapshot versions.

The explicit similarity declaration is:

```json
{
  "metric": "cosine",
  "tile_reduce": "max",
  "tie_break": "block_index_ascending",
  "score_range": [-1.0, 1.0]
}
```

Each alignment artifact records all tile scores, best block, feature versions, vocabulary versions, and both upstream hashes.

### Node `ranking`

Inputs: all pair alignment hashes, pair set, `min_score`, `top_k`, tie-break declaration, feature/vocab snapshot versions.

Sort key:

```text
score descending
pair_id natural ascending
best block index ascending
pair_id bytewise ascending
```

Metrics include total candidates, eligible candidates, returned hits, max/mean eligible score, threshold, and `top_k`.

## 3. Content-Addressed Cache

Each cache file is an envelope:

```json
{
  "key": "...",
  "node": "visual",
  "pair_id": "p03",
  "key_components": {},
  "versions": {},
  "artifact_hash": "...",
  "payload": {},
  "checksum": "..."
}
```

`artifact_hash` is the canonical JSON checksum of `payload`; `checksum` protects the entire envelope. A cache hit verifies both. Existing keys are never overwritten with different content.

Key scope:

| Node | Input summary | Upstream product | Version scope |
|---|---|---|---|
| `decode` | seed, shape, rect, texture cell | none | decoder |
| `visual` | feature params | decode hash | feature snapshot |
| `text` | text, locale | none | vocabulary snapshot |
| `align` | cosine declaration | visual + text hashes | feature + vocabulary |
| `ranking` | retrieval config, pair ids, tie-break | all align hashes | feature + vocabulary |

This scoping is deliberate: changing a grid does not invalidate decode, and editing one text does not invalidate another pair's image/text caches.

`query_cache(node, pair_id)` returns the complete key components, envelope checksum, artifact hash, and versions. For the global ranking artifact, a `pair_facet` and `pair_facet_hash` are included for the requested pair.

## 4. Incremental Invalidation

The previous manifest records sample fields, snapshot checksums, node keys, and artifact hashes. On each run:

1. Compare current samples/config/snapshots with the prior manifest.
2. Derive affected sets per local node.
3. Compute prospective cache keys from upstream hashes.
4. Reuse every matching cache entry.
5. Recompute only misses.
6. Re-aggregate the global ranking.

Examples:

| Change | decode | visual | text | align | ranking |
|---|---|---|---|---|---|
| text of p02 | reuse | reuse | p02 | p02 | global |
| seed of p03 | p03 | p03 | reuse | p03 | global |
| grid/histogram | reuse | all | reuse | all | global |
| threshold/top_k | reuse | reuse | reuse | reuse | global |

The output includes `recomputed_nodes`, `reused_nodes`, per-node affected pair IDs, a local pair-ID union, and explicit reasons. Unchanged pairs are not marked affected.

## 5. Hit Lineage and Replay

Every hit records:

- `hit_id`, rank, canonical score, best block;
- source path `decode -> visual -> text -> align -> ranking`;
- every cache key and artifact hash along that path;
- vocabulary snapshot version/checksum/revision;
- feature snapshot version/checksum/revision;
- similarity declaration and ranking tie-break.

A persistent hit index maps `hit_id` to the retrieval cache key. Replay:

1. Checks whether recorded vocabulary or feature versions still match current versioned snapshots.
2. Rejects mismatches with `E_LINEAGE_BROKEN` and reports the earliest changed dependency by registry revision.
3. Reloads visual/text/alignment caches by their recorded keys.
4. Recomputes the max-cosine score and best block.
5. Rebuilds ranking from recorded eligible scores and compares the complete order.

No mutable "latest result" is used for historical replay; old cache entries and the hit index retain provenance.

## 6. Snapshots and Four-Phase State Machine

Stages:

1. `visual_features` (decode + visual features)
2. `text_embeddings`
3. `alignment_scores`
4. `retrieval_ranking`

Each committed stage file contains:

- stage name/index, run ID;
- per-stage monotonic version;
- complete sorted `completed_pair_ids`;
- per-pair cache key/artifact hash views;
- lineage metadata;
- canonical content checksum.

A stage is committed only when all pair items are present. Writes use a same-directory temporary file, `fsync`, and atomic `os.replace`. A `.part`/`.tmp-*` file is never treated as committed and is discarded during recovery.

Each run binds the current input digest in `current_run.json`. On restart:

- if the bound digest matches and the run is incomplete, continue from the first uncommitted stage;
- if input changed externally, start a new run;
- already committed stages are verified by artifact hash and are not re-committed.

## 7. Versioned Registries

`vocab`, `features`, and `retrieval_result` use immutable archive files plus an atomic `current.json` pointer:

```text
snapshots/registry/<kind>/
  current.json
  v00000001-<payload-checksum>.json
  v00000002-<payload-checksum>.json
```

- Version numbers are monotonic.
- Every document carries a whole-content checksum.
- A new document replaces the current pointer only after a complete archive write.
- Lower-version writes return `E_SNAPSHOT_ROLLBACK`.
- Historical content cannot be reintroduced as a new version; it also returns `E_SNAPSHOT_ROLLBACK`.
- Same-version different content is treated as a whole replacement with a fresh monotonic version.
- Corrupt current/archive documents return `E_SNAPSHOT_CORRUPT`.
- A registry-wide high-water mark records the largest observed version per kind. Restoring an older valid `current.json` physically also returns `E_SNAPSHOT_ROLLBACK`.

## 8. Concurrency

Incremental execution acquires `snapshots/submission.lock` with `O_CREAT|O_EXCL`.

- Another live owner causes immediate `E_STAGE_BUSY`.
- A dead owner on the same host is reclaimed.
- A lock from another host is not reclaimed automatically.
- The lock file stores token, PID, host, operation, and acquisition time.
- Release removes the lock only if the token still matches.

## 9. Validation and Resource Limits

Validation happens before mutation/execution:

- duplicate `pair_id` -> `E_DUPLICATE_PAIR_ID`;
- shape mismatch with fixed canvas -> `E_IMAGE_SHAPE_MISMATCH`;
- invalid seed -> `E_IMAGE_SEED_INVALID`;
- empty text -> `E_TEXT_EMPTY`;
- non-positive grid/histogram/texture cell -> `E_GRID_INVALID`;
- invalid rectangle/locale/config -> corresponding structured code;
- missing token -> `E_VOCAB_MISSING`;
- out-of-range token index -> `E_VOCAB_OUT_OF_RANGE`.

Limits:

- `max_pixel_bytes_per_image`: checked before decoding/reuse, returns `E_RESOURCE_LIMIT` with pair/field;
- `max_cache_entries`: checked before adding a missing entry;
- `max_recompute_pairs`: checked for affected local pairs and actual recomputed pair set.

## 10. Self-Check and Interactive UI

`selfcheck` verifies cache envelope/artifact checksums, stage checksums, manifest checksum, manifest-to-cache links, current registry versions, ranking snapshot linkage, and hit-index cache links.

The CLI (`status`, `topology`, `hits`, `trace`, `replay`, `selfcheck`, `recover`) and REPL display:

- DAG topology;
- last-run invalidation coverage and recompute/reuse counts;
- per-hit rank/score/block/hit ID and full JSON lineage;
- stage versions/checksums and registry versions;
- the latest structured business error code.
