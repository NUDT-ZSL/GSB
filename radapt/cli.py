from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import RadaptError
from .engine import RuntimeEngine
from .parsing import load_sample_bundle
from .sample import FIXED_GLOBAL_SEED, write_sample_files
from .storage import RuntimeStorage


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radapt", description="Deterministic streaming window reoptimization runtime")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate fixed-seed sample inputs and columnar batches")
    generate.add_argument("--out", default="samples/deterministic")
    generate.add_argument("--seed", type=int, default=FIXED_GLOBAL_SEED)

    validate = sub.add_parser("validate", help="parse and validate a sample directory")
    validate.add_argument("--sample", default="samples/deterministic")

    run = sub.add_parser("run", help="run or recover the engine against SQLite state")
    run.add_argument("--sample", default="samples/deterministic")
    run.add_argument("--db", default="artifacts/radapt.sqlite3")
    run.add_argument("--report", default="artifacts/run-report.json")
    run.add_argument("--crash-after-batch", type=int, default=None)
    run.add_argument("--crash-mode", choices=("sigkill", "raise"), default="sigkill")

    recover = sub.add_parser("recover", help="print SQLite recovery snapshot")
    recover.add_argument("--db", default="artifacts/radapt.sqlite3")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = write_sample_files(args.out, args.seed)
            _print_json({"ok": True, "out_dir": result["out_dir"], "batch_count": result["batch_count"]})
            return 0

        if args.command == "validate":
            bundle = load_sample_bundle(args.sample)
            _print_json(
                {
                    "ok": True,
                    "tables": [table.name for table in bundle.tables],
                    "windows": [window.window_id for window in bundle.plan.windows],
                    "batches": len(bundle.arrivals.batches),
                    "fixed_point_scale": bundle.config.fixed_point_scale,
                }
            )
            return 0

        if args.command == "recover":
            storage = RuntimeStorage(args.db)
            try:
                _print_json({"ok": True, "recovery": storage.recovery_snapshot()})
            finally:
                storage.close()
            return 0

        if args.command == "run":
            bundle = load_sample_bundle(args.sample)
            engine = RuntimeEngine(
                bundle,
                args.db,
                sample_dir=args.sample,
                crash_after_batch=args.crash_after_batch,
                crash_mode=args.crash_mode,
            )
            try:
                report = engine.run()
                report["ok"] = not report.get("business_error_codes")
                report["recovery_snapshot_final"] = engine.recovery_snapshot()
            finally:
                engine.close()
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
            _print_json(
                {
                    "ok": report["ok"],
                    "report": str(report_path),
                    "processed": report["processed_batches"],
                    "skipped": report["skipped_committed_batches"],
                    "drift_events": len(report["drift_events"]),
                    "switch_decisions": [
                        {
                            "switched": item.get("switched"),
                            "status": item.get("status"),
                            "error_code": item.get("error_code"),
                            "selected": item.get("selected_assessment", {}).get("form_id") if item.get("switched") else None,
                            "precision_sensitive": item.get("precision_sensitive"),
                        }
                        for item in report["switch_decisions"]
                    ],
                    "truth_verification": report["truth_verification"],
                    "business_error_codes": report["business_error_codes"],
                    "recovery": report["recovery_snapshot_final"],
                }
            )
            return 0 if report["ok"] else 3

        return 1
    except RadaptError as exc:
        _print_json(exc.to_dict())
        return 2 if exc.code.startswith("E_") and "BUDGET_INSUFFICIENT" not in exc.code else 3
    except FileNotFoundError as exc:
        _print_json({"ok": False, "error": {"code": "E_FILE_NOT_FOUND", "message": str(exc)}})
        return 2


if __name__ == "__main__":
    sys.exit(main())
