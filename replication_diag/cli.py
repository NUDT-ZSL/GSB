"""Command-line interface for load/merge/lag/resume/status/verify."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .errors import DiagnosticsError
from .file_lock import exclusive_file_lock
from .injections import run_injection_manifest
from .pipeline import load_sources
from .state import State
from .util import stable_json_text
from .v2_parser import parse_v2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "sample.json"
DEFAULT_INJECTIONS = PROJECT_ROOT / "config" / "injections.json"
DEFAULT_DB = PROJECT_ROOT / "runtime" / "state.sqlite"


def _add_common(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_default else str(DEFAULT_DB)
    parser.add_argument(
        "--db",
        default=default,
        help=f"SQLite state database (default: {DEFAULT_DB})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="replication-diag",
        description="Deterministic local database replication-lag diagnostics.",
    )
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and stage local position samples")
    _add_common(load_parser, suppress_default=True)
    load_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    load_parser.add_argument("--package", help="v2 package manifest to stage")
    load_parser.add_argument("--inject", help="run one configured injection without persisting")
    load_parser.add_argument("--injections", default=str(DEFAULT_INJECTIONS))

    merge_parser = subparsers.add_parser("merge", help="finalize the oldest staged operation")
    _add_common(merge_parser, suppress_default=True)

    lag_parser = subparsers.add_parser("lag", help="emit the committed lag report")
    _add_common(lag_parser, suppress_default=True)

    resume_parser = subparsers.add_parser("resume", help="resume all staged operations")
    _add_common(resume_parser, suppress_default=True)

    status_parser = subparsers.add_parser("status", help="emit active snapshot status")
    _add_common(status_parser, suppress_default=True)

    verify_parser = subparsers.add_parser("verify", help="verify persisted state and output")
    _add_common(verify_parser, suppress_default=True)
    return parser


def _with_state(db_path: str, action: Callable[[State], dict[str, Any]]) -> dict[str, Any]:
    path = Path(db_path).resolve()
    with exclusive_file_lock(path.with_name(path.name + ".lock")):
        with State(path) as state:
            return action(state)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "load":
        if args.inject:
            return run_injection_manifest(args.config, args.injections, args.inject)
        config = load_config(args.config)
        if args.package:
            entries, manifest = parse_v2(Path(args.package), config)
            return _with_state(
                args.db,
                lambda state: state.stage_package(
                    Path(args.package).resolve(), entries, manifest
                ),
            )
        bundle = load_sources(config)
        return _with_state(args.db, lambda state: state.stage_source(bundle, config))
    if args.command == "merge":
        return _with_state(args.db, lambda state: state.merge_pending())
    if args.command == "lag":
        return _with_state(args.db, lambda state: state.lag_output())
    if args.command == "resume":
        return _with_state(args.db, lambda state: state.resume())
    if args.command == "status":
        return _with_state(args.db, lambda state: state.status())
    if args.command == "verify":
        return _with_state(args.db, lambda state: state.verify())
    raise DiagnosticsError("E_STATE", message=f"unknown command {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        sys.stdout.buffer.write(stable_json_text(result).encode("utf-8"))
        return 0 if result.get("ok", True) else 1
    except DiagnosticsError as exc:
        sys.stdout.buffer.write(stable_json_text(exc.to_dict()).encode("utf-8"))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
