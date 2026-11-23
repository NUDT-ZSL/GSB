"""Deterministic local sample generation.

Produces, from a fixed master seed:
  * tasks.jsonl  - inference task manifest
  * vocab.json   - token vocabulary mapping (with version)
  * config.json  - scheduler / budget configuration

Logits are NOT materialized on disk: they are regenerated on the fly by
the sampling core from each request's seed (see sampling.py), which keeps
samples small and fully deterministic.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np

MASTER_SEED = 20261124
VOCAB_VERSION = "v1"


def generate(out_dir: str, n_requests: int = 24, vocab_size: int = 256,
             master_seed: int = MASTER_SEED) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence([master_seed])))

    vocab = {
        "version": VOCAB_VERSION,
        "size": vocab_size,
        "tokens": [f"tok_{i}" for i in range(vocab_size)],
    }
    with open(os.path.join(out_dir, "vocab.json"), "w", encoding="utf-8") as fh:
        json.dump(vocab, fh, ensure_ascii=False, indent=2)

    tasks = []
    for i in range(n_requests):
        tasks.append({
            "request_id": f"req-{i:04d}",
            "prompt_tokens": int(rng.integers(8, 128)),
            "sampling_params": {
                "temperature": round(float(rng.uniform(0.5, 1.5)), 3),
                "top_k": int(rng.choice([8, 16, 32, 64])),
                "top_p": round(float(rng.uniform(0.8, 1.0)), 3),
            },
            "target_len": int(rng.integers(8, 48)),
            "seed": int(rng.integers(1, 2**31 - 1)),
            "priority": int(rng.integers(0, 5)),
        })
    with open(os.path.join(out_dir, "tasks.jsonl"), "w", encoding="utf-8") as fh:
        for t in tasks:
            fh.write(json.dumps(t, ensure_ascii=False) + "\n")

    config = {
        "max_slots": 4,
        "batch_size": 4,
        "max_waves": 8,
        "step_budget": 720,
        "kv_budget": 900,
        "candidate_budget": 48,
        "topp_floor": 0.5,
        "tradeoff_priority": ["memory", "progress", "quality"],
    }
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    return {"vocab_size": vocab_size, "n_requests": n_requests, "out_dir": out_dir}


def main(argv=None):
    ap = argparse.ArgumentParser(description="generate deterministic local samples")
    ap.add_argument("--out", default="data")
    ap.add_argument("--requests", type=int, default=24)
    ap.add_argument("--vocab-size", type=int, default=256)
    ap.add_argument("--seed", type=int, default=MASTER_SEED)
    args = ap.parse_args(argv)
    info = generate(args.out, args.requests, args.vocab_size, args.seed)
    print(json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
