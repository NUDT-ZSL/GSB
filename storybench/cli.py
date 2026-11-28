"""Command-line entry points for generation, runs, and repeatability checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .generate import DEFAULT_SEED, generate_workspace, load_data
from .pipeline import (
    build_pipeline,
    validate_pipeline,
    write_pipeline_outputs,
)


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _require_data(data_dir: Path) -> None:
    required = [
        "stories.json",
        "grading_levels.json",
        "lexicon.json",
        "model_corpus.json",
    ]
    missing = [name for name in required if not (data_dir / name).is_file()]
    if missing:
        raise SystemExit(
            f"missing generated data in {data_dir}: {', '.join(missing)}; "
            "run `python3 generate_data.py` first"
        )


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_workspace(args.data_dir, args.seed)
    _print_json(
        {
            "status": "generated",
            "data_dir": str(Path(args.data_dir)),
            "seed": manifest["generator"]["seed"],
            "story_count": manifest["story_count"],
            "level_count": manifest["level_count"],
        }
    )
    return 0


def _write_run(data: dict[str, Any], out_dir: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = build_pipeline(data)
    validation = validate_pipeline(data, run)
    run["summary"]["acceptance"] = {
        "passed": validation["passed"],
        "error_count": len(validation["errors"]),
    }
    write_pipeline_outputs(run, out_dir)
    return run, validation


def command_run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    _require_data(data_dir)
    data = load_data(data_dir)
    run, validation = _write_run(data, args.out_dir)
    result = {
        "status": "ok" if validation["passed"] else "failed_acceptance",
        "out_dir": str(Path(args.out_dir)),
        "summary": run["summary"],
        "acceptance": validation,
    }
    _print_json(result)
    return 0 if validation["passed"] else 1


def command_verify(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    _require_data(data_dir)
    data = load_data(data_dir)
    first = build_pipeline(data)
    second = build_pipeline(data)
    first_validation = validate_pipeline(data, first)
    first["summary"]["acceptance"] = {
        "passed": first_validation["passed"],
        "error_count": len(first_validation["errors"]),
    }
    first_hash = first["summary"]["payload_sha256"]
    second_hash = second["summary"]["payload_sha256"]
    verification = {
        "checker": "repeatability-v1",
        "runs": 2,
        "first_payload_sha256": first_hash,
        "second_payload_sha256": second_hash,
        "identical_payloads": first_hash == second_hash,
        "pipeline_validation": first_validation,
        "passed": first_hash == second_hash and first_validation["passed"],
    }
    manifest = write_pipeline_outputs(first, args.out_dir, verification)
    _print_json(
        {
            "status": "ok" if verification["passed"] else "failed",
            "verification": verification,
            "manifest": manifest,
        }
    )
    return 0 if verification["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic graded-story rewriting and exercise workbench"
    )
    parser.add_argument(
        "--data-dir", default="data", help="directory containing generated JSON data"
    )
    parser.add_argument(
        "--out-dir", default="outputs", help="directory for audit artifacts"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="generation seed (used by the generate command)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generate", help="write the deterministic story corpus")
    subparsers.add_parser("run", help="run the pipeline and write all artifacts")
    subparsers.add_parser("verify", help="run twice and verify identical outputs")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        return command_generate(args)
    if args.command == "run":
        return command_run(args)
    if args.command == "verify":
        return command_verify(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
