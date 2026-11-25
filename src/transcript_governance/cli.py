"""Command line entry points."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .diagnose import run_diagnostic
from .samples import DEFAULT_GAIN, DEFAULT_SEED, generate_sample_tree


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="transcript-governance")
    subcommands = parser.add_subparsers(dest="command", required=True)

    generate = subcommands.add_parser("generate", help="generate fixed-seed PCM and v1/v2 samples")
    generate.add_argument("--out", default="samples", type=Path)
    generate.add_argument("--seed", default=DEFAULT_SEED, type=int)
    generate.add_argument("--gain", default=DEFAULT_GAIN, type=float)

    diagnose = subcommands.add_parser("diagnose", help="run end-to-end diagnostics and write reports")
    diagnose.add_argument("--samples", default="samples", type=Path)
    diagnose.add_argument("--reports", default="reports", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        paths = generate_sample_tree(args.out, seed=args.seed, gain=args.gain)
        print(json.dumps(paths.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "diagnose":
        report = run_diagnostic(args.samples, args.reports)
        print(report["markdown_report"])
        print(f"JSON report: {report['json_report']}")
        return 0 if report["overall_ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
