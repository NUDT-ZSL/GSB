"""Command-line entry points for the OCR laboratory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .samples import generate_workspace
from .serialization import canonical_json, read_json
from .service import WorkspaceService
from .templates import get_template_library


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _service(args: argparse.Namespace) -> WorkspaceService:
    return WorkspaceService(
        args.data_dir,
        args.state_dir,
        page_count=args.pages,
        base_seed=args.seed,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ocrlab",
        description="Deterministic local OCR data editing and pipeline laboratory",
    )
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--state-dir", default=".ocr_state")
    parser.add_argument("--pages", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261123)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="generate deterministic page samples")
    init_parser.add_argument("--force", action="store_true")

    run_parser = subparsers.add_parser("run", help="run the four-stage pipeline")
    run_parser.add_argument("--reset", action="store_true")
    run_parser.add_argument(
        "--injections",
        help="path to a JSON file containing a list of injection specs",
    )
    run_parser.add_argument("--full-report", action="store_true")

    demo_parser = subparsers.add_parser("demo", help="run a deterministic demonstration")
    demo_parser.add_argument(
        "name",
        choices=["injection", "downgrade", "recovery"],
    )

    subparsers.add_parser("state", help="print persisted workspace state")
    subparsers.add_parser("report", help="print the latest report")
    subparsers.add_parser("injection-log", help="print the latest injection log")
    subparsers.add_parser("selfcheck", help="print the latest self-check")

    upgrade_parser = subparsers.add_parser("upgrade", help="upgrade the template library")
    upgrade_parser.add_argument("--version", default="glyph-v2")

    serve_parser = subparsers.add_parser("serve", help="run the local web application")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    verify_parser = subparsers.add_parser(
        "verify",
        help="run the in-process acceptance verification",
    )
    verify_parser.add_argument(
        "--output",
        default="artifacts/verification.json",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            records = generate_workspace(
                args.data_dir,
                page_count=args.pages,
                base_seed=args.seed,
                library=get_template_library("glyph-v1"),
                force=args.force,
            )
            _print(
                {
                    "ok": True,
                    "pages": len(records),
                    "page_ids": [record["page_id"] for record in records],
                    "data_dir": str(Path(args.data_dir).resolve()),
                }
            )
            return 0
        if args.command == "serve":
            from .server import create_server

            server = create_server(
                args.host,
                args.port,
                data_dir=args.data_dir,
                state_dir=args.state_dir,
            )
            print(f"OCR Laboratory listening on http://{args.host}:{args.port}")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
            return 0
        if args.command == "verify":
            from .verify import run_verification

            result = run_verification(Path(args.output))
            _print(result)
            return 0 if result["ok"] else 1

        service = _service(args)
        if args.command == "run":
            injections: list[dict[str, Any]] = []
            if args.injections:
                injections = read_json(args.injections)
                if not isinstance(injections, list):
                    raise BusinessError(
                        "E_INJECTION_INVALID",
                        "injection file must contain a JSON list",
                        stage="cli",
                        field="injections",
                    )
            result = service.run(injections, reset=args.reset)
            if args.full_report:
                _print(result.report)
            else:
                _print(result.summary())
            return 0
        if args.command == "demo":
            if args.name == "injection":
                result = service.demo_injection()
                _print(result.summary())
            elif args.name == "downgrade":
                result = service.demo_downgrade()
                _print(result.summary())
            else:
                _print(service.demo_recovery())
            return 0
        if args.command == "state":
            _print(service.state())
            return 0
        if args.command == "report":
            _print(service.report())
            return 0
        if args.command == "injection-log":
            _print(service.injection_log())
            return 0
        if args.command == "selfcheck":
            state = service.state()
            selfcheck_path = Path(args.state_dir) / "artifacts" / "selfcheck.json"
            if selfcheck_path.exists():
                _print(read_json(selfcheck_path))
            else:
                _print(state.get("selfcheck") or {"ok": False, "message": "not run"})
            return 0
        if args.command == "upgrade":
            result = service.upgrade_templates(args.version)
            _print(result.runtime.get("upgrade", {}))
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except BusinessError as error:
        print(canonical_json({"ok": False, "error": error.to_dict()}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

