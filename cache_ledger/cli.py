from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .errors import CacheLedgerError
from .models import load_bundle
from .runner import execute_command, resume
from .samplegen import generate_samples
from .store import LedgerStore, canonical_json
from .verify import verify_replay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cache-ledger",
        description="Deterministic multi-tenant tiered cache ledger adjudication engine",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--samples", "--workspace", dest="samples", default="samples", help="deterministic sample directory")
    common.add_argument("--db", "--state", dest="db", default="state/cache_ledger.sqlite3", help="SQLite ledger path")
    common.add_argument("--clock-ms", type=int, default=None, help="inject logical clock milliseconds")
    common.add_argument("--clock-tolerance-ms", type=int, default=None)
    common.add_argument("--report", default=None, help="optional path to write full JSON result")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("load", parents=[common], help="parse/validate samples and initialize SQLite ledger")

    merge = sub.add_parser("merge", parents=[common], help="merge v1/v2 policies and compute closure")
    merge.add_argument("--patch", dest="patch_path", default=None, help="optional policy patch JSON")

    admit = sub.add_parser("admit", parents=[common], help="admit entries and adjudicate target tiers")
    admit.add_argument("--tenant", default=None)

    rebalance = sub.add_parser("rebalance", parents=[common], help="schedule cooldown-aware tier migrations")
    rebalance.add_argument("--tenant", default=None)
    rebalance.add_argument("--time-budget-ms", type=int, default=None)
    rebalance.add_argument("--fail-key", default=None, help="test key that fails after logical start")

    evict = sub.add_parser("evict", parents=[common], help="evict tombstones and quota/pressure victims")
    evict.add_argument("--tenant", default=None)

    sub.add_parser("resume", parents=[common], help="recover prepared operations and interrupted commands")
    sub.add_parser("status", parents=[common], help="stable sorted JSON ledger status")
    sub.add_parser("verify", parents=[common], help="check invariants and global replay equivalence")

    generate = sub.add_parser("generate-samples", parents=[common], help="generate deterministic local samples")
    generate.add_argument("--seed", type=int, default=436202611)
    generate.add_argument("--out", default=None)
    return parser


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def write_report(path: Optional[str], payload: Any) -> None:
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def request_from_args(args: argparse.Namespace) -> dict[str, Any]:
    request: dict[str, Any] = {"command": args.command}
    for key in ("clock_ms", "clock_tolerance_ms", "tenant", "patch_path", "time_budget_ms", "fail_key"):
        if hasattr(args, key):
            request[key] = getattr(args, key)
    return request


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate-samples":
            out = args.out or args.samples
            manifest = generate_samples(out, args.seed)
            result = {"ok": True, "generated": out, "manifest": manifest}
            print_json(result)
            write_report(args.report, result)
            return 0

        if args.command == "status":
            store = LedgerStore(args.db)
            try:
                result = store.status()
                print_json(result)
                write_report(args.report, result)
            finally:
                store.close()
            return 0

        bundle = load_bundle(args.samples)

        if args.command == "load":
            LedgerStore.reset_database(args.db)
            store = LedgerStore(args.db)
            try:
                summary = store.load_bundle(bundle)
                result = {"ok": True, "command": "load", "db": args.db, "summary": summary}
                print_json(result)
                write_report(args.report, result)
            finally:
                store.close()
            return 0

        store = LedgerStore(args.db)
        try:
            if args.command == "verify":
                result = verify_replay(store, bundle)
                print_json(result)
                write_report(args.report, result)
                return 0

            if args.command == "resume":
                result = resume(store, bundle)
                print_json(result)
                write_report(args.report, result)
                return 0

            request = request_from_args(args)
            result = execute_command(store, bundle, request)
            output = {key: value for key, value in result.items() if key not in ("view",)}
            if args.command == "merge":
                output["unified_policy_count"] = len(result["view"]["policies"])
                output["policy_fingerprint"] = result["view"]["policy_fingerprint"]
            print_json(output)
            write_report(args.report, result)
            return 0
        finally:
            store.close()

    except CacheLedgerError as exc:
        payload = exc.to_dict()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return exc.exit_code
    except FileNotFoundError as exc:
        error = CacheLedgerError("E_FILE_NOT_FOUND", str(exc))
        print_json(error.to_dict())
        return 2
    except Exception as exc:
        error = CacheLedgerError("E_INTERNAL", f"{type(exc).__name__}: {exc}")
        print(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
