"""Compile a config version evolution into a per-node distribution plan."""


def compile_plan(manifest):
    """Return {'version', 'strategy', 'assignments': {node_id: {key: value}}}."""
    targets = list(manifest.targets)
    items = dict(manifest.items)
    strategy = manifest.strategy

    if strategy["mode"] == "full":
        assignments = {node: dict(items) for node in targets}
    else:  # shard
        shard_count = strategy["shards"]
        keys = sorted(items)
        shards = [[] for _ in range(shard_count)]
        for pos, key in enumerate(keys):
            shards[pos % shard_count].append(key)
        assignments = {node: {} for node in targets}
        for shard_idx, shard_keys in enumerate(shards):
            node = targets[shard_idx % len(targets)]
            for key in shard_keys:
                assignments[node][key] = items[key]

    return {
        "version": manifest.version,
        "strategy": dict(strategy),
        "assignments": assignments,
    }
