from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, canonical_text
from .config import AuditConfig
from .incremental import IncrementalManager
from .pipeline import AuditPipeline
from .query import QueryIndex
from .sample_generator import write_sample_bundle
from .verification import verify_deterministic_foundations


def _print(value: Any) -> None:
    print(canonical_text(value) if not isinstance(value, str) else value)


def cmd_generate_sample(args: argparse.Namespace) -> int:
    paths = write_sample_bundle(args.out, AuditConfig())
    _print({name: str(path) for name, path in paths.items()})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pipeline = AuditPipeline(args.workspace, AuditConfig())
    result = pipeline.run_full(revision_id=args.revision)
    pcm, _manifest = pipeline.ensure_sample()
    foundation = verify_deterministic_foundations(
        pcm, result["v1_rules"], result["turn_points"], pipeline.config
    )
    atomic_write_json(pipeline.reports_dir / "foundation_verification.json", foundation)
    summary = {
        "ok": result["self_check"]["ok"] and foundation["ok"],
        "workspace": str(pipeline.workspace),
        "events": len(result["turn_points"]),
        "segments": len(result["segments"]),
        "ledger_rows": len(result["ledger_rows"]),
        "anomalies": [
            {
                "event_id": row["event_id"],
                "at_ms": row["at_ms"],
                "code": row["business_code"],
            }
            for row in result["classified_turns"]
            if row["anomaly_type"] != "normal_switch"
        ],
        "self_check_ok": result["self_check"]["ok"],
        "foundation_ok": foundation["ok"],
        "ledger_dir": str(pipeline.ledger_dir),
        "reports_dir": str(pipeline.reports_dir),
    }
    _print(summary)
    return 0 if summary["ok"] else 2


def cmd_query(args: argparse.Namespace) -> int:
    pipeline = AuditPipeline(args.workspace, AuditConfig())
    result = pipeline.load_last_result()
    index = QueryIndex(result, pipeline.config)
    if args.at_ms is not None:
        response = index.at_ms(args.at_ms)
    elif args.segment is not None:
        response = index.segment(args.segment)
    else:
        raise SystemExit("query requires --at-ms or --segment")
    _print(response)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if args.deep:
        from .scenarios import run_deep_verification

        report = run_deep_verification(Path(args.workspace) / "deep_verify")
        _print(
            {
                "ok": report["ok"],
                "workspace": str(Path(args.workspace) / "deep_verify"),
                "steps": report["steps"],
            }
        )
        return 0 if report["ok"] else 2

    pipeline = AuditPipeline(args.workspace, AuditConfig())
    result = pipeline.load_last_result()
    pcm, _manifest = pipeline.ensure_sample()
    foundation = verify_deterministic_foundations(
        pcm, result["v1_rules"], result["turn_points"], pipeline.config
    )
    report = {
        "ok": result["self_check"]["ok"] and foundation["ok"],
        "self_check": result["self_check"],
        "foundation": foundation,
    }
    _print({"ok": report["ok"], "error_codes": result["self_check"].get("error_codes", [])})
    return 0 if report["ok"] else 2


def cmd_demo(args: argparse.Namespace) -> int:
    from .webapp import serve

    serve(args.workspace, host=args.host, port=args.port)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-audit")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-sample")
    generate.add_argument("--out", default="data/sample")
    generate.set_defaults(func=cmd_generate_sample)

    run = sub.add_parser("run")
    run.add_argument("--workspace", default="workspace/demo")
    run.add_argument("--revision", default="r0")
    run.set_defaults(func=cmd_run)

    query = sub.add_parser("query")
    query.add_argument("--workspace", default="workspace/demo")
    target = query.add_mutually_exclusive_group(required=True)
    target.add_argument("--at-ms")
    target.add_argument("--segment")
    query.set_defaults(func=cmd_query)

    verify = sub.add_parser("verify")
    verify.add_argument("--workspace", default="workspace/demo")
    verify.add_argument("--deep", action="store_true")
    verify.set_defaults(func=cmd_verify)

    demo = sub.add_parser("demo")
    demo.add_argument("--workspace", default="workspace/demo")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8765)
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
