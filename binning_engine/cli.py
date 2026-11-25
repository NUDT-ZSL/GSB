"""Command line interface for the deterministic binning engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from .codes import EngineError
from .config_migration import migrate_v1_to_v2, migrate_v2_to_v1
from .hashing import canonical_dumps, sha256_file
from .pipeline import run_pipeline


def _print_json(value: Any) -> None:
    print(canonical_dumps(value))


def _summary(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "output_dir": result["output_dir"],
        "run_id": result["run_id"],
        "row_count": len(result["encoding_result"]["rows"]),
        "result_hash": result["encoding_result"]["result_hash"],
        "statistics_hash": result["statistics"]["statistics_hash"],
        "workers": result["workers"],
        "batch_size": result["batch_size"],
        "comparison_count": result["comparison_count"],
        "recovered_batches": result["recovered_batches"],
        "resource_code": result["resource_plan"]["code"],
        "resource_decisions": result["resource_plan"]["decisions"],
        "self_check_ok": result["self_check"]["ok"],
        "dispatch_matches_merge": result["scheduler_order"]["dispatch_matches_merge"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m binning_engine",
        description="Deterministic tabular binning/encoding diagnostic engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run validation, binning, encoding and export")
    run.add_argument("--samples", required=True, type=Path)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--cache-dir", type=Path)
    run.add_argument("--workers", type=int)
    run.add_argument("--batch-size", type=int)
    run.add_argument("--serial", action="store_true", help="run batches in this process")
    run.add_argument("--kill-after-batch", type=int)
    run.add_argument("--keep-going", action="store_true")

    v1v2 = subparsers.add_parser("migrate-v1-v2")
    v1v2.add_argument("--input", required=True, type=Path)
    v1v2.add_argument("--output", required=True, type=Path)

    v2v1 = subparsers.add_parser("migrate-v2-v1")
    v2v1.add_argument("--input", required=True, type=Path, help="v2 snapshot directory")
    v2v1.add_argument("--output", required=True, type=Path)

    verify = subparsers.add_parser("verify-output")
    verify.add_argument("--output", required=True, type=Path)

    return parser


def verify_output(output_dir: Path) -> Dict[str, Any]:
    manifest_path = output_dir / "output_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for item in manifest["files"]:
        path = output_dir / item["path"]
        if not path.exists():
            failures.append({"path": item["path"], "reason": "missing"})
            continue
        actual = sha256_file(path)
        if actual != item["sha256"]:
            failures.append(
                {"path": item["path"], "reason": "checksum_mismatch", "actual": actual}
            )
    encoding = json.loads((output_dir / "encoding_result.json").read_text(encoding="utf-8"))
    stats = json.loads((output_dir / "statistics.json").read_text(encoding="utf-8"))
    return {
        "ok": not failures,
        "failures": failures,
        "result_hash": encoding.get("result_hash"),
        "statistics_hash": stats.get("statistics_hash"),
        "file_count": len(manifest["files"]),
    }


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_pipeline(
                args.config,
                samples_path=args.samples,
                output_dir=args.output,
                cache_dir=args.cache_dir,
                workers=1 if args.serial and args.workers is None else args.workers,
                batch_size=args.batch_size,
                use_process_pool=not args.serial,
                kill_after_batch=args.kill_after_batch,
                keep_going=args.keep_going,
            )
            _print_json(_summary(result))
            return 0
        if args.command == "migrate-v1-v2":
            metadata = migrate_v1_to_v2(args.input, args.output)
            _print_json({"ok": True, "migration": "v1_to_v2", **metadata})
            return 0
        if args.command == "migrate-v2-v1":
            v1 = migrate_v2_to_v1(args.input, args.output)
            _print_json(
                {
                    "ok": True,
                    "migration": "v2_to_v1",
                    "version": v1["version"],
                    "relative_seconds": v1["recorded_at_relative_seconds"],
                }
            )
            return 0
        if args.command == "verify-output":
            report = verify_output(args.output)
            _print_json(report)
            return 0 if report["ok"] else 3
        raise EngineError("E_CLI", f"unknown command: {args.command}")
    except EngineError as error:
        _print_json({"ok": False, "error": error.to_dict()})
        return 2


if __name__ == "__main__":
    sys.exit(main())
