from __future__ import annotations

import argparse
import json
import sys

from .generator import DEFAULT_SEED
from .pipeline import analyze_dataset, generate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic ancient-book layout analysis pipeline."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="generate deterministic samples")
    _add_common_paths(generate_parser)
    generate_parser.add_argument("--pages", type=int, default=3)

    analyze_parser = subparsers.add_parser("analyze", help="analyze an existing sample directory")
    _add_common_paths(analyze_parser)

    all_parser = subparsers.add_parser("all", help="generate samples and analyze them")
    _add_common_paths(all_parser)
    all_parser.add_argument("--pages", type=int, default=3)
    return parser


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--samples", default="artifacts/samples")
    parser.add_argument("--out", default="artifacts/results")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        manifest = generate_dataset(args.samples, seed=args.seed, page_count=args.pages)
        print(
            json.dumps(
                {
                    "command": "generate",
                    "seed": manifest["seed"],
                    "pages": len(manifest["pages"]),
                    "samples_dir": args.samples,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "analyze":
        results = analyze_dataset(args.samples, args.out)
        print(
            json.dumps(
                {
                    "command": "analyze",
                    "seed": results["seed"],
                    "summary": results["summary"],
                    "results_dir": results["results_dir"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "all":
        generate_dataset(args.samples, seed=args.seed, page_count=args.pages)
        results = analyze_dataset(args.samples, args.out)
        print(
            json.dumps(
                {
                    "command": "all",
                    "seed": results["seed"],
                    "summary": results["summary"],
                    "results_dir": results["results_dir"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
