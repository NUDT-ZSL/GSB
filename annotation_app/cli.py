"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .demo import run_demo
from .samplegen import write_assets
from .service import WorkbenchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m annotation_app",
        description="Local deterministic relation/event annotation workbench",
    )
    parser.add_argument("--root", default=".", help="workspace root (default: .)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("generate", help="regenerate deterministic samples/snapshots")
    demo_parser = subparsers.add_parser("demo", help="run the full reproducible demo")
    demo_parser.add_argument("--json", action="store_true", help="print the full report")

    validate_parser = subparsers.add_parser(
        "validate", help="run demo and report validation status"
    )
    validate_parser.add_argument("--json", action="store_true")

    show_parser = subparsers.add_parser("show", help="print one format view")
    show_parser.add_argument(
        "--view", choices=("v1", "v2", "line"), default="v2"
    )

    ui_parser = subparsers.add_parser("ui", help="start the local web application")
    ui_parser.add_argument("--host", default="127.0.0.1")
    ui_parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    if args.command == "generate":
        result = write_assets(root)
        print(
            json.dumps(
                {
                    "status": "generated",
                    "seed": result["seed"],
                    "sample_id": result["sample_id"],
                    "text": result["text"],
                    "files": {
                        "v1": result["v1"],
                        "v2": result["v2"],
                        "line": result["line"],
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command in {"demo", "validate"}:
        report = run_demo(root)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": report.get("status"),
                        "report_checksum": report.get("report_checksum"),
                        "report": str(root / "cache" / "demo-report.json"),
                        "migration_v1": report.get("migration_roundtrip", {})
                        .get("v1_path", {})
                        .get("status"),
                        "migration_v2": report.get("migration_roundtrip", {})
                        .get("v2_path", {})
                        .get("status"),
                        "line_roundtrip": report.get("line_roundtrip", {}).get(
                            "status"
                        ),
                        "audit_replay": report.get("edit_session", {}).get("status"),
                        "ontology_strict_blocked": report.get(
                            "ontology_contraction", {}
                        )
                        .get("strict", {})
                        .get("blocked"),
                        "ontology_affected": report.get(
                            "ontology_contraction", {}
                        )
                        .get("lenient", {})
                        .get("affected_count"),
                        "unattributed": report.get("unattributed", []),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0 if report.get("status") == "ok" else 2
    if args.command == "show":
        service = WorkbenchService(root)
        content, _ = service.view_content(args.view)
        if isinstance(content, str):
            sys.stdout.write(content)
        else:
            print(json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "ui":
        from .web import run_server

        run_server(root, host=args.host, port=args.port)
        return 0
    return 1

