# Sample formats

All sample paths are workspace-relative. No sample contains wall-clock values: all timestamps are fixed constants or derived from `clock.json`.

## v1

`v1/snapshot.json`:

```json
{
  "format": "v1",
  "snapshot_version": 2,
  "logical_clock_ms": 1710000002000,
  "nodes": [
    {
      "id": "n1",
      "label": "person",
      "weight": 1.5,
      "relative_write_seconds": 1,
      "adjacency": ["n2", "follows", "n3", "friend"],
      "out_edges": [
        {"target": "n2", "label": "follows"},
        {"target": "n3", "label": "friend"}
      ]
    }
  ],
  "linear_index": ["n1"]
}
```

`adjacency` and `out_edges` must agree in order and multiplicity. `linear_index` must be strictly sorted and cover every node exactly once. v1 edges have no explicit edge weight, so migration assigns `1.0`.

v1 absolute node time is:

```text
anchor_ms + floor(relative_write_seconds * scale_numerator / scale_denominator)
```

For `samples/clock.json`, the anchor is `1710000000000`, scale is `1000/1`, and rounding is `floor`.

## v2 manifest

`v2/manifest.json` lists shards and their file-relative paths, generations and content hashes:

```json
{
  "format": "v2",
  "snapshot_version": 3,
  "logical_clock_ms": 1710000006000,
  "metadata_db": "meta.sqlite",
  "adjacency_digest": "<sha256>",
  "shards": [
    {
      "shard_id": "shard-000",
      "generation": 4,
      "path": "shards/shard-000.json",
      "content_hash": "<sha256>"
    }
  ]
}
```

Each shard document contains `format: "v2-shard"`, `shard_id`, `generation`, `content_hash`, and nodes. The content hash is SHA-256 over canonical JSON with the `content_hash` field omitted. Canonical JSON uses UTF-8, sorted object keys, no insignificant whitespace.

A v2 node contains:

```json
{
  "canonical_id": "n1",
  "label": "person",
  "written_at_ms": 1710000004500,
  "generation": 4,
  "tombstone": false,
  "state": "active",
  "out_edges": [
    {"target": "n2", "label": "follows", "weight": 0.8}
  ]
}
```

`state` must be `active` or `tombstone` and must agree with the boolean `tombstone` field. Node generation must equal shard generation. Shard generations in manifest order must be monotonic.

## v2 metadata

`v2/meta.sqlite` uses:

- `meta(key, value)`: `format`, `snapshot_version`, `logical_clock_ms`, `global_generation`, `adjacency_digest`.
- `shards(ordinal, shard_id, generation, content_hash, node_count, edge_count, adjacency_digest)`.

The metadata is opened read-only during validation. Any metadata mismatch is a business error.

## Packages

A package manifest uses `schema: "graph-package/v1"` and declares:

- package ID/version and package logical clock;
- SHA-256 for v1 snapshot, v2 manifest and v2 metadata DB;
- every v2 shard path and file SHA-256;
- merged node-count bounds and merged adjacency digest;
- `change_kind: "full"` or `"append"`.

Validation reads all inputs and computes the complete merged index before the store is allowed to write. A bad shard hash or missing shard therefore rejects the whole package.

## Injection manifest

`injection.json` is a machine-readable list of base load, rollback rejection, clock rollback rejection, corrupt/missing shard rejection and crash/resume checks. Test failpoints are enabled only with:

```text
GRAPHSTORE_ENABLE_TEST_FAILPOINTS=1
GRAPHSTORE_CRASH_AT=<point>
```

Supported crash points include `replace_after_build_row`, `replace_after_stage_nodes`, `replace_after_stage_edges`, `replace_before_finalize`, `traverse_after_prepare`, `traverse_after_layer_1`, and `traverse_before_commit`.

