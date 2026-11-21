from __future__ import annotations

import argparse
from typing import Any, Sequence

from .diagnostics import report_from_db, status_from_db, verify_from_db
from .engine import advance, resume
from .errors import BusinessError
from .jsonutil import atomic_write_text, pretty_json
from .spec import load_workspace
from .store import load_database


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m batchdiag",
        description=(
            "Local dynamic-batching precision spectrum diagnostics workbench. "
            "All paths are resolved from the current working directory."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate samples and create a fresh state DB")
    load.add_argument("--model", required=True, help="model description JSON")
    load.add_argument("--replicas", required=True, help="replica list JSON")
    load.add_argument("--requests", required=True, help="request trace JSON")
    load.add_argument("--config", required=True, help="precision diagnostics config JSON")
    load.add_argument("--db", required=True, help="SQLite state database path")
    load.add_argument(
        "--force", action="store_true", help="replace an existing state database"
    )

    advance_parser = subparsers.add_parser(
        "advance", help="advance logical time under the dispatch quota"
    )
    advance_parser.add_argument("--db", required=True)
    advance_parser.add_argument("--steps", type=int, default=None)
    advance_parser.add_argument(
        "--to", type=int, default=None, dest="to_time", help="process steps through this clock"
    )

    spectrum = subparsers.add_parser(
        "spectrum", help="emit the stable per-batch precision spectrum report"
    )
    spectrum.add_argument("--db", required=True)
    spectrum.add_argument("--output", default=None, help="also write the report JSON here")

    resume_parser = subparsers.add_parser(
        "resume", help="repair incomplete commits, then continue advancing"
    )
    resume_parser.add_argument("--db", required=True)
    resume_parser.add_argument("--steps", type=int, default=None)
    resume_parser.add_argument("--to", type=int, default=None, dest="to_time")

    status = subparsers.add_parser("status", help="show persistent workbench state")
    status.add_argument("--db", required=True)

    verify = subparsers.add_parser(
        "verify", help="verify persisted state against spectrum and optional report"
    )
    verify.add_argument("--db", required=True)
    verify.add_argument("--report", default=None, help="diagnostic report JSON to compare")
    return parser


def _success(command: str, data: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "code": "OK",
        "command": command,
        "data": data,
    }


def _resolve_steps(args: argparse.Namespace) -> int:
    if args.steps is not None and args.to_time is not None:
        raise BusinessError(
            "E_USAGE", "use either --steps or --to, not both", {}
        )
    if args.steps is None and args.to_time is None:
        return 1
    return 1 if args.steps is None else args.steps


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "load":
        workspace = load_workspace(
            args.model, args.replicas, args.requests, args.config
        )
        data = load_database(args.db, workspace, force=args.force)
        return _success("load", data)

    if args.command == "advance":
        data = advance(
            args.db,
            steps=_resolve_steps(args),
            to_time=args.to_time,
        )
        return _success("advance", data)

    if args.command == "resume":
        if args.steps is not None and args.to_time is not None:
            raise BusinessError("E_USAGE", "use either --steps or --to, not both", {})
        data = resume(args.db, steps=args.steps, to_time=args.to_time)
        return _success("resume", data)

    if args.command == "spectrum":
        report = report_from_db(args.db)
        if args.output:
            atomic_write_text(args.output, pretty_json(report) + "\n")
        return _success("spectrum", report)

    if args.command == "status":
        return _success("status", status_from_db(args.db))

    if args.command == "verify":
        verified = verify_from_db(args.db, args.report)
        return _success("verify", verified)

    raise BusinessError("E_USAGE", "unknown command", {"command": args.command})


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        output = _run(args)
        print(pretty_json(output))
        return 0
    except BusinessError as exc:
        print(pretty_json(exc.as_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        print(
            pretty_json(
                {
                    "ok": False,
                    "code": "E_INTERRUPTED",
                    "message": "interrupted before command completion",
                    "details": {},
                }
            )
        )
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(
            pretty_json(
                {
                    "ok": False,
                    "code": "E_INTERNAL",
                    "message": "unexpected internal failure",
                    "details": {"type": type(exc).__name__, "error": str(exc)},
                }
            )
        )
        return 1
