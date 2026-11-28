"""Command-line entry point for the domain vocabulary pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .pipeline import PipelineConfig, run_pipeline


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-vocab",
        description=(
            "Deterministically extend a domain vocabulary across v1/v2/v3 encodings, "
            "initialize new embeddings, adapt frozen base embeddings, and report checks."
        ),
    )
    parser.add_argument("--seed", type=_non_negative_int, default=202411)
    parser.add_argument(
        "--vocab-version",
        "--version",
        dest="vocab_version",
        choices=("v1", "v2", "v3", "all"),
        default="all",
        help="version used for the primary extension; 'all' audits all and uses v1",
    )
    parser.add_argument(
        "--samples",
        "--sample-size",
        "--num-samples",
        dest="samples",
        type=_positive_int,
        default=168,
    )
    parser.add_argument("--embedding-dim", "--dim", type=_positive_int, default=24)
    parser.add_argument("--epochs", type=_positive_int, default=80)
    parser.add_argument("--learning-rate", type=_positive_float, default=0.03)
    parser.add_argument("--cosine-threshold", type=float, default=0.6)
    parser.add_argument("--uncertain-tolerance", type=float, default=1e-3)
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the same JSON document printed to stdout",
    )
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = PipelineConfig(
            seed=args.seed,
            vocab_version=args.vocab_version,
            samples=args.samples,
            embedding_dim=args.embedding_dim,
            epochs=args.epochs,
            learning_rate=args.learning_rate,
            cosine_threshold=args.cosine_threshold,
            uncertain_tolerance=args.uncertain_tolerance,
        )
        result = run_pipeline(config)
    except ValueError as error:
        parser.error(str(error))

    if args.compact:
        document = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    else:
        document = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document + "\n", encoding="utf-8")
    sys.stdout.write(document + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

