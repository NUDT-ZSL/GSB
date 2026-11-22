# Deterministic local samples

- `origin_manifest.json`: loopback origin resources and cache policy.
- `nodes.json`: edge nodes, capacity, per-round validation budget.
- `v1_snapshot.json`: generation 1 relative-seconds cache snapshot.
- `v2/shards/*.json`: generation 2 shards with absolute logical clocks.
- `v2_meta.sqlite`: shard metadata and raw-byte SHA-256 hashes.
- `injections.json`: parse, merge, freshness, and origin anomalies.
- `policy.json`: logical clock, tolerances, retry budget, scale threshold.

All file references are relative to this directory. Regenerate with
`python scripts/generate_samples.py`.
