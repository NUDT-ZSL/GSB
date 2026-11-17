# Deterministic sample inputs

Run `python scripts/generate_samples.py --out samples` from the repository root to
regenerate these files. All identifiers and digests are derived from seed
`20261117`; there are no external downloads or cluster dependencies.

- `train_config.yaml`: 3-layer, hidden-size 8, vocabulary 96 integer model,
  2 KiB logical shard limit, 4 workers, 4 concurrent writes, 36,000-byte budget,
  and 12 training rounds.
- `data_shards.jsonl`: 16 stable data-shard descriptions.
- `scheduler_policy.yaml`: occupancy/remaining-round rules for retention,
  merge mode, write priority, and budget action.
- `injections.json`: independent samples for out-of-range occupancy, illegal
  retention, oversized shard, merge exception, fingerprint conflict,
  generation exception, and write exception.

Run the normal path without injections:

```bash
.venv/bin/python -m shardcheck run --samples samples --run-dir runs/baseline --fresh
```

Run the fault-injection path:

```bash
.venv/bin/python -m shardcheck run --samples samples --run-dir runs/injected \
  --injections samples/injections.json --fresh
```
