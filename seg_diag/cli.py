"""Command line entry points."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import DiagnosticError
from .pipeline import run_diagnostic_pipeline, write_report
from .sample_generator import DEFAULT_SEED, generate_sample_directory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seg-diag", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--workdir", default=".")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--force", action="store_true")

    diagnose = sub.add_parser("diagnose", help="run the full diagnostic pipeline")
    diagnose.add_argument("--workdir", default=".")
    diagnose.add_argument("--seed", type=int, default=DEFAULT_SEED)
    diagnose.add_argument("--no-regenerate", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            path = generate_sample_directory(
                Path(args.workdir) / "samples" / f"local_seed_{args.seed}",
                seed=args.seed,
                force=args.force,
            )
            print(f"generated deterministic sample at {path}")
            return 0
        if args.command == "diagnose":
            report = run_diagnostic_pipeline(
                args.workdir, seed=args.seed, regenerate=not args.no_regenerate
            )
            report_path = Path(args.workdir) / "reports" / "diagnostic_report.json"
            print(f"business_code={report['business_code']}")
            print(f"report={report_path}")
            print(f"ledger_version={report['semantic_ledger']['ledger_version']}")
            print(f"ledger_checksum={report['semantic_ledger']['checksum']}")
            print(
                "mapping_recompute="
                f"{report['mapping_revision']['plan']['coverage_check']['recompute_count']} "
                f"reuse={report['mapping_revision']['plan']['coverage_check']['reuse_count']}"
            )
            print(f"dominant_error={report['error_spectrum']['dominant_error_source']}")
            return 0
    except DiagnosticError as exc:
        error_path = Path(args.workdir) / "reports" / "error_report.json"
        try:
            write_report(error_path, {"ok": False, **exc.to_dict()})
            print(f"{exc.code}: {exc.message}", file=sys.stderr)
            print(f"error_report={error_path}", file=sys.stderr)
        except Exception:
            print(str(exc), file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
