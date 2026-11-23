"""Generate the deterministic local corpus (fixed seed, no network)."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt_pipeline.datagen import generate_corpus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data")
    ap.add_argument("--seed", type=int, default=20240501)
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--vocab-size", type=int, default=512)
    args = ap.parse_args()
    manifest = generate_corpus(args.out, seed=args.seed, n_pairs=args.n,
                               vocab_size=args.vocab_size)
    print(f"generated {manifest['n_pairs']} pairs in {args.out}")
    for name, digest in manifest["files"].items():
        print(f"  {name}: {digest[:16]}...")


if __name__ == "__main__":
    main()
