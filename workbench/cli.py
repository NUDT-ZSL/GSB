from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import WorkbenchError
from .ioformats import load_observations_jsonl, load_registry
from .pipeline import (
    cmd_init_registry,
    cmd_load_observations,
    cmd_load_shard_manifest,
    cmd_merge,
    cmd_patch,
    cmd_reconcile,
    cmd_resume,
    cmd_status,
    cmd_status_verbose,
)
from .rules import load_v1_rules, load_v2_bundle, merge_rule_sources
from .seed import BASE_CLOCK_MS, generate_sample
from .engine import source_health_diff
from .verification import verify_sources


def _print_json(value: Any, *, pretty: bool = True) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    sys.stdout.write(text)


def _write_output(path: str | None, value: Any, *, pretty: bool = True) -> None:
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if pretty:
            output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        else:
            output.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _add_db_and_clock(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default="artifacts/workbench.db", help="SQLite ledger path")
    parser.add_argument("--clock-ms", type=int, default=int(os.environ.get("WB_CLOCK_MS", BASE_CLOCK_MS)), help="injected logical clock in milliseconds")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workbench", description="Registry and health probe diagnostics workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("generate-sample", help="generate deterministic local sample")
    p_seed.add_argument("--out", default="samples")
    p_seed.add_argument("--seed", type=int, default=20261126)

    p_load = sub.add_parser("load", help="validate and load registry, observations, or shard manifest")
    _add_db_and_clock(p_load)
    p_load.add_argument("--registry")
    p_load.add_argument("--observations", action="append", default=[])
    p_load.add_argument("--shard-manifest")
    p_load.add_argument("--only-shard", help="load only one shard_id from the manifest")

    p_merge = sub.add_parser("merge", help="merge v1/v2 rule snapshots and emit decision chain")
    _add_db_and_clock(p_merge)
    p_merge.add_argument("--v1")
    p_merge.add_argument("--v2-manifest")
    p_merge.add_argument("--registry-for-diff")
    p_merge.add_argument("--observations-for-diff")
    p_merge.add_argument("--out")

    p_reconcile = sub.add_parser("reconcile", help="differentially reconcile buffered observations")
    _add_db_and_clock(p_reconcile)

    p_patch = sub.add_parser("patch", help="submit an incremental rule patch")
    _add_db_and_clock(p_patch)
    p_patch.add_argument("--patch", required=True)

    p_resume = sub.add_parser("resume", help="recover buffered half-committed observations")
    _add_db_and_clock(p_resume)

    p_status = sub.add_parser("status", help="print stable sorted health ledger JSON")
    p_status.add_argument("--db", default="artifacts/workbench.db")
    p_status.add_argument("--out")
    p_status.add_argument("--compact", action="store_true")
    p_status.add_argument("--verbose", action="store_true", help="include persisted shard/buffer operational state")

    p_verify = sub.add_parser("verify", help="bitwise/canonical ledger comparison and consistency verification")
    p_verify.add_argument("--expected")
    p_verify.add_argument("--expected-db")
    p_verify.add_argument("--actual", action="append", default=[])
    p_verify.add_argument("--actual-db", action="append", default=[])
    p_verify.add_argument("--self-test", action="store_true", help="run deterministic order/crash/concurrency proof")

    p_serve = sub.add_parser("serve", help="run minimal local Flask workbench UI")
    p_serve.add_argument("--db", default="artifacts/workbench.db")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=5050)

    return parser


def _run_merge(args: argparse.Namespace) -> dict[str, Any]:
    view = cmd_merge(
        args.db,
        v1_path=args.v1,
        v2_manifest_path=args.v2_manifest,
        clock_ms=args.clock_ms,
    )
    output: dict[str, Any] = {"ok": True, "rule_view": view, "merge_decisions": view["merge_decisions"]}
    if args.registry_for_diff and args.observations_for_diff:
        registry = load_registry(args.registry_for_diff)
        records = load_observations_jsonl(args.observations_for_diff)
        v1 = load_v1_rules(args.v1) if args.v1 else None
        v2 = load_v2_bundle(args.v2_manifest) if args.v2_manifest else None
        output["health_diff"] = source_health_diff(
            registry,
            records,
            merge_rule_sources(v1, None, injection_clock_ms=args.clock_ms) if v1 else None,
            merge_rule_sources(None, v2, injection_clock_ms=args.clock_ms) if v2 else None,
            view,
        )
    if args.out:
        _write_output(args.out, output, pretty=True)
    return output


def run_command(args: argparse.Namespace) -> Any:
    if args.command == "generate-sample":
        return {"ok": True, "sample": generate_sample(args.out, args.seed)}

    if args.command == "load":
        output: dict[str, Any] = {"ok": True, "results": []}
        if args.registry:
            output["results"].append({"registry": cmd_init_registry(args.db, args.registry, args.clock_ms)})
        for observations in args.observations:
            output["results"].append({"observations": cmd_load_observations(args.db, observations)})
        if args.shard_manifest:
            output["results"].append(
                {"shards": cmd_load_shard_manifest(args.db, args.shard_manifest, only_shard=args.only_shard)}
            )
        if not args.registry and not args.observations and not args.shard_manifest:
            raise WorkbenchError("E_LOAD_ARGS", "load requires --registry, --observations, or --shard-manifest", {})
        return output

    if args.command == "merge":
        return _run_merge(args)

    if args.command == "reconcile":
        return {"ok": True, "result": cmd_reconcile(args.db, args.clock_ms)}

    if args.command == "patch":
        return {"ok": True, **cmd_patch(args.db, args.patch, args.clock_ms)}

    if args.command == "resume":
        return {"ok": True, "result": cmd_resume(args.db, args.clock_ms)}

    if args.command == "status":
        ledger = cmd_status_verbose(args.db) if args.verbose else cmd_status(args.db)
        if args.out:
            _write_output(args.out, ledger, pretty=not args.compact)
        return ledger

    if args.command == "verify":
        if args.self_test:
            from .acceptance import run_acceptance

            report = run_acceptance("artifacts/verify-self-test-report.json")
            if not report.get("ok"):
                raise WorkbenchError("E_RESULT_MISMATCH", "verify self-test failed", report)
            return report
        return verify_sources(
            expected_path=args.expected,
            expected_db=args.expected_db,
            actual_paths=args.actual,
            actual_dbs=args.actual_db,
        )

    if args.command == "serve":
        from .webapp import create_app

        app = create_app(args.db)
        app.run(host=args.host, port=args.port, debug=False)
        return {"ok": True}

    raise WorkbenchError("E_COMMAND_UNKNOWN", f"unknown command: {args.command}", {})


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_command(args)
        if args.command != "serve":
            _print_json(result, pretty=not (args.command == "status" and getattr(args, "compact", False)))
        return 0
    except WorkbenchError as exc:
        _print_json(exc.to_dict(), pretty=True)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        details = {"type": type(exc).__name__, "message": str(exc)}
        if os.environ.get("WB_DEBUG"):
            import traceback

            details["traceback"] = traceback.format_exc()
        _print_json({"ok": False, "error": {"code": "E_INTERNAL_ERROR", "message": str(exc), "details": details}}, pretty=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
