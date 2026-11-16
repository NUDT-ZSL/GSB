"""Command line entry: sample generation and pipeline execution."""
from __future__ import annotations

import argparse
import sys

from . import VERSION


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mmef", description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("gen", help="generate deterministic local samples")
    p_gen.add_argument("--out", required=True, help="output data directory")
    p_gen.add_argument("--num-samples", type=int, default=8)
    p_gen.add_argument("--seed", type=int, default=None)

    p_run = sub.add_parser("run", help="run the fusion pipeline")
    p_run.add_argument("--data", required=True, help="data directory")
    p_run.add_argument("--out", required=True, help="output directory")
    p_run.add_argument("--concurrency", type=int, default=None,
                       help="override config concurrency")

    args = parser.parse_args(argv)
    if args.cmd == "gen":
        from .samplegen import SEED, generate

        path = generate(
            args.out,
            num_samples=args.num_samples,
            seed=args.seed if args.seed is not None else SEED,
        )
        print(f"samples generated at {path}")
        return 0
    from .runner import execute

    return execute(args.data, args.out, concurrency=args.concurrency)


if __name__ == "__main__":
    sys.exit(main())
