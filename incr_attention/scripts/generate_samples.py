"""Generate deterministic local Q/K/V samples, edit scripts and references.

No external data and no GPU: every tensor comes from a seeded NumPy default
rng and is quantized to a small decimal grid so runs are exactly repeatable.
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from incrattn.reference import reference_full  # noqa: E402


def tensor(rng, rows, head_dim, low=-2.0, high=2.0):
    raw = rng.uniform(low, high, size=(rows, head_dim))
    return np.round(raw, decimals=3).astype(np.float32)


def save_dataset(name, n_q, head_dim, block_size, edits, seed):
    base = os.path.join(ROOT, "samples", name)
    blocks_dir = os.path.join(base, "blocks")
    os.makedirs(blocks_dir, exist_ok=True)
    rng = np.random.default_rng(seed)
    queries = tensor(rng, n_q, head_dim)
    np.savez(os.path.join(blocks_dir, "q.npz"), queries=queries)

    manifest = {
        "dataset": name,
        "seed": seed,
        "query_length": n_q,
        "head_dim": head_dim,
        "block_size": block_size,
        "scale": 1.0 / float(np.sqrt(head_dim)),
        "edits": [],
    }
    kv_parts_k, kv_parts_v = [], []
    for edit in edits:
        if edit["op"] == "append":
            rows = edit["rows"]
            key_block = tensor(rng, rows, head_dim)
            value_block = tensor(rng, rows, head_dim, low=-1.5, high=1.5)
            tag = f"append_{len(kv_parts_k):02d}"
            np.savez(
                os.path.join(blocks_dir, f"{tag}.npz"),
                key_block=key_block,
                value_block=value_block,
            )
            kv_parts_k.append(key_block)
            kv_parts_v.append(value_block)
            manifest["edits"].append({"op": "append", "file": f"{tag}.npz"})
        elif edit["op"] == "replace":
            target = edit["kv_block_index"]
            start = target * block_size
            rows = min(block_size, n_q - start)
            key_block = tensor(rng, rows, head_dim, low=-1.8, high=1.8)
            value_block = tensor(rng, rows, head_dim, low=-1.8, high=1.8)
            tag = f"replace_{target:02d}"
            np.savez(
                os.path.join(blocks_dir, f"{tag}.npz"),
                key_block=key_block,
                value_block=value_block,
            )
            full_k = np.concatenate(kv_parts_k, axis=0)
            full_v = np.concatenate(kv_parts_v, axis=0)
            full_k[start : start + rows] = key_block
            full_v[start : start + rows] = value_block
            kv_parts_k = [full_k]
            kv_parts_v = [full_v]
            manifest["edits"].append({
                "op": "replace",
                "kv_block_index": target,
                "file": f"{tag}.npz",
            })
        else:
            raise ValueError(f"unknown edit op {edit['op']}")

    keys = np.concatenate(kv_parts_k, axis=0).astype(np.float32)
    values = np.concatenate(kv_parts_v, axis=0).astype(np.float32)
    np.savez(
        os.path.join(blocks_dir, "final_kv.npz"), keys=keys, values=values
    )

    scale = manifest["scale"]
    reference = reference_full(queries, keys, values, block_size, scale, "full")
    padded_row_max = np.zeros((len(reference), block_size), dtype=np.float32)
    padded_exp_sum = np.zeros_like(padded_row_max)
    padded_output = np.zeros(
        (len(reference), block_size, head_dim), dtype=np.float32
    )
    valid_rows = np.zeros(len(reference), dtype=np.int64)
    for item in reference:
        rows = item.row_max.shape[0]
        valid_rows[item.block_index] = rows
        padded_row_max[item.block_index, :rows] = item.row_max
        padded_exp_sum[item.block_index, :rows] = item.exp_sum
        padded_output[item.block_index, :rows, :] = item.output
    np.savez(
        os.path.join(blocks_dir, "reference.npz"),
        row_max=padded_row_max,
        exp_sum=padded_exp_sum,
        output=padded_output,
        valid_rows=valid_rows,
        fingerprints=np.array(
            [item.fingerprint for item in reference], dtype=np.dtype("U64")
        ),
    )
    manifest["final_kv_length"] = int(keys.shape[0])
    manifest["output_blocks"] = len(reference)
    with open(os.path.join(base, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return manifest


def main():
    datasets = [
        {
            "name": "small",
            "n_q": 10,
            "head_dim": 4,
            "block_size": 4,
            "seed": 20261117,
            "edits": [
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 2},
            ],
        },
        {
            "name": "medium",
            "n_q": 18,
            "head_dim": 8,
            "block_size": 4,
            "seed": 424242,
            "edits": [
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 2},
            ],
        },
        {
            "name": "edit_heavy",
            "n_q": 16,
            "head_dim": 6,
            "block_size": 4,
            "seed": 987654,
            "edits": [
                {"op": "append", "rows": 4},
                {"op": "append", "rows": 4},
                {"op": "replace", "kv_block_index": 1},
                {"op": "append", "rows": 4},
                {"op": "replace", "kv_block_index": 0},
                {"op": "append", "rows": 4},
            ],
        },
    ]
    summaries = []
    for spec in datasets:
        summaries.append(save_dataset(**spec))
    print(json.dumps([
        {
            "dataset": item["dataset"],
            "output_blocks": item["output_blocks"],
            "final_kv_length": item["final_kv_length"],
        }
        for item in summaries
    ], indent=2))


if __name__ == "__main__":
    main()
