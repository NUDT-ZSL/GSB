from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import AuditError
from .pipeline import regenerate_audit, replay_coordinate, run_audit, write_failure_trace
from .sample import generate_sample


def _print_error(error: AuditError, failure_trace: str | None = None) -> int:
    payload = {"ok": False, "error": error.to_dict()}
    if failure_trace:
        payload["failure_trace"] = failure_trace
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="panoptic-audit", description="Panoptic mask active probe and ledger audit engine")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-sample", help="generate deterministic local sample")
    generate.add_argument("--out", default="samples/fixed_seed")

    run = sub.add_parser("run", help="parse truth, execute probes, commit ledger and write audit")
    run.add_argument("--config", default="configs/run_config.json")
    run.add_argument("--fresh", action="store_true", help="replace local ledger snapshot")

    audit = sub.add_parser("audit", help="regenerate audit report from committed ledger")
    audit.add_argument("--config", default="configs/run_config.json")

    verify = sub.add_parser("verify", help="run cross-module consistency verification")
    verify.add_argument("--config", default="configs/run_config.json")

    replay = sub.add_parser("replay", help="independently replay one committed coordinate")
    replay.add_argument("--config", default="configs/run_config.json")
    replay.add_argument("--x", type=int, required=True)
    replay.add_argument("--y", type=int, required=True)
    replay.add_argument("--ledger-revision", type=int, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate-sample":
            manifest = generate_sample(Path(args.out))
            print(json.dumps({"ok": True, "sample": args.out, "seed": manifest["seed"]}, ensure_ascii=False))
            return 0
        if args.command == "run":
            report = run_audit(args.config, fresh=args.fresh)
            print(json.dumps({
                "ok": True,
                "ledger_revision": report["ledger"]["ledger_revision"],
                "probes": len(report["probes"]),
                "failures": len(report["failures"]),
                "self_check": report["self_check"]["status"],
                "report_checksum": report["report_checksum"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"audit", "verify"}:
            report = regenerate_audit(args.config)
            print(json.dumps({
                "ok": True,
                "command": args.command,
                "ledger_revision": report["ledger"]["ledger_revision"],
                "probes": len(report["probes"]),
                "self_check": report["self_check"],
                "report_checksum": report["report_checksum"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "replay":
            result = replay_coordinate(args.config, args.x, args.y, args.ledger_revision)
            record = result["record"]
            print(json.dumps({
                "ok": True,
                "coordinate": result["coordinate"],
                "ledger_version": result["ledger_version"],
                "requested_ledger_revision": result["requested_ledger_revision"],
                "business_code": record["business_code"],
                "status": record["status"],
                "ledger_result": record["ledger_result"],
                "execution": record["execution"],
                "timing": record["timing"],
            }, ensure_ascii=False, indent=2))
            return 0
    except AuditError as error:
        failure_trace = None
        if getattr(args, "config", None):
            try:
                failure_trace = str(write_failure_trace(args.config, error))
            except Exception:
                failure_trace = None
        return _print_error(error, failure_trace)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
