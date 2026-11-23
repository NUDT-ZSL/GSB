"""Command line entry point for VocalForge Local."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .demo import print_demo_summary, run_demo
from .service import LabService
from .web import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vocalforge",
        description="VocalForge Local deterministic speech modeling lab",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="workspace root for artifacts, snapshots, and reports",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="start the local web application")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--no-browser", action="store_true")

    subparsers.add_parser("demo", help="run the complete repeatable validation demo")
    subparsers.add_parser("check", help="run cross-level consistency self-check")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.command == "demo":
        report = run_demo(root)
        print_demo_summary(report)
        return 0 if report["ok"] else 1
    if args.command == "check":
        service = LabService(root)
        result = service.run_consistency()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    if args.command == "serve":
        service = LabService(root)
        serve(
            service,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
