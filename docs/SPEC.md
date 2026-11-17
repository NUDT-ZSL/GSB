# Replica Group Specification

## Top-level fields

```json
{
  "schema_version": 1,
  "group_id": "sample-grid-perturbation",
  "digest_algorithm": "sha256",
  "digest_length": 64,
  "nodes": [],
  "checks": [],
  "repair_policy": {}
}
```

Only `sha256` / length `64` is accepted. The complete document is rejected with
`E_SPEC` if any field is invalid. `details.errors` contains every sorted field-path
error when collection is possible.

## Node and block fields

```json
{
  "id": "n1",
  "directory": "nodes/n1",
  "priority": 10,
  "repair_load": 3,
  "blocks": [
    {
      "block_id": "B-SINGLE",
      "byte_size": 1536,
      "shards": [
        {"index": 0, "offset": 0, "byte_size": 512, "digest": "<64 hex>"}
      ]
    }
  ]
}
```

Validation rules:

- node identifiers non-empty and unique;
- node directories unique after resolution;
- priority: positive integer (booleans rejected);
- repair load: non-negative integer when present;
- block identifier unique within a node;
- byte size: positive integer;
- shards non-empty, indices contiguous from zero, offsets contiguous, shard sizes positive;
- shard size sum equals block size;
- shard digest is exactly 64 hexadecimal characters;
- replicas of the same logical block agree on byte size and shard layout, but may
  intentionally carry different shard digests/content.

Block identifiers are logical and may be reused across nodes to represent replicas.

## Checks

```json
{"block_id":"B-TIE","depends_on":["B-GRID"]}
```

Every referenced block must exist. Duplicate check ids, self-dependencies,
duplicate dependencies, and cycles are rejected. Dependency meaning is “this block
validates after/with the listed prerequisite blocks”; only cycle freedom is used by
the core repair flow.

## Repair policy

```json
{
  "alert_ratio": {"numerator": 1, "denominator": 8},
  "cost": {
    "transfer_weight": 1,
    "work_weight": 1,
    "priority_weight": 1,
    "load_weight": 1,
    "transfer_overhead_numerator": 3,
    "transfer_overhead_denominator": 100,
    "work_numerator": 3,
    "work_denominator": 2
  }
}
```

All weights and denominators are positive integers. The alert numerator is
positive; transfer overhead numerator is non-negative. Errors carry paths such as
`repair_policy.cost.load_weight`.
