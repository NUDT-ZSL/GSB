from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any, Sequence

from .engine import DriftEngine
from .errors import EngineError
from .jsonutil import read_json, stable_json
from .store import StateStore
from .validation import load_bundle


DEFAULT_DB = ".kdrift/state.db"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"SQLite state database (default: {DEFAULT_DB})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kdrift",
        description=(
            "Deterministic local Kubernetes desired-state drift "
            "convergence engine"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser(
        "load", help="validate and load deterministic JSON samples"
    )
    load_parser.add_argument("--expected")
    load_parser.add_argument("--actual")
    load_parser.add_argument("--config")
    load_parser.add_argument(
        "--sample",
        help="directory containing expected.json, actual.json, config.json",
    )
    _common(load_parser)

    diff_parser = subparsers.add_parser(
        "diff", help="compute deterministic desired/actual drift"
    )
    _common(diff_parser)

    plan_parser = subparsers.add_parser(
        "plan", help="build dependency-aware convergence plan"
    )
    _common(plan_parser)

    apply_parser = subparsers.add_parser(
        "apply", help="execute convergence rounds"
    )
    apply_parser.add_argument("--max-rounds", type=int, default=200)
    apply_parser.add_argument(
        "--crash-at",
        help=(
            "deterministic crash injection, for example "
            "operation-start:2 or round-commit:1"
        ),
    )
    _common(apply_parser)

    resume_parser = subparsers.add_parser(
        "resume", help="recover persisted state and continue convergence"
    )
    resume_parser.add_argument("--max-rounds", type=int, default=200)
    resume_parser.add_argument("--crash-at")
    _common(resume_parser)

    status_parser = subparsers.add_parser(
        "status", help="show stable object phases and clock watermark"
    )
    _common(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify persisted state and convergence conclusion"
    )
    _common(verify_parser)
    return parser


def _emit(value: Any) -> None:
    sys.stdout.write(stable_json(value))
    sys.stdout.flush()


def _sample_paths(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.sample:
        return (
            args.expected or os.path.join(args.sample, "expected.json"),
            args.actual or os.path.join(args.sample, "actual.json"),
            args.config or os.path.join(args.sample, "config.json"),
        )
    missing = [
        name
        for name in ("expected", "actual", "config")
        if getattr(args, name) is None
    ]
    if missing:
        raise EngineError(
            "E_BAD_CLI",
            "load requires --sample or all of --expected, --actual, --config",
            details={"missing": missing},
        )
    return args.expected, args.actual, args.config


def command_load(args: argparse.Namespace) -> int:
    expected_path, actual_path, config_path = _sample_paths(args)
    expected_document = read_json(expected_path)
    actual_document = read_json(actual_path)
    config_document = read_json(config_path, code="E_BAD_CONFIG")
    desired, actual, config, issues = load_bundle(
        expected_document, actual_document, config_document
    )
    with StateStore(args.db) as store:
        store.initialize(desired, actual, config, issues)
    _emit(
        {
            "ok": True,
            "database": args.db,
            "loaded": {
                "desired_objects": len(desired),
                "actual_objects": len(actual),
                "validation_issues": len(issues),
            },
            "config": config.to_dict(),
            "issues": [issue.to_dict() for issue in issues],
        }
    )
    return 0


def _with_engine(
    args: argparse.Namespace, operation: Any
) -> tuple[int, Any]:
    with StateStore(args.db) as store:
        engine = DriftEngine(
            store, getattr(args, "crash_at", None)
        )
        return operation(engine)


def command_diff(args: argparse.Namespace) -> int:
    def run(engine: DriftEngine) -> tuple[int, Any]:
        result = engine.diff()
        return 0, {"ok": True, **result}

    code, output = _with_engine(args, run)
    _emit(output)
    return code


def command_plan(args: argparse.Namespace) -> int:
    def run(engine: DriftEngine) -> tuple[int, Any]:
        result = engine.plan()
        return (0 if result["valid"] else 1), result

    code, output = _with_engine(args, run)
    _emit(output)
    return code


def _apply_command(
    args: argparse.Namespace, *, resume: bool
) -> int:
    def run(engine: DriftEngine) -> tuple[int, Any]:
        result = engine.apply(
            max_rounds=args.max_rounds, resume=resume
        )
        verification = engine.verify()
        result["verification"] = verification
        return (0 if result["converged"] else 1), result

    code, output = _with_engine(args, run)
    _emit(output)
    return code


def command_apply(args: argparse.Namespace) -> int:
    return _apply_command(args, resume=False)


def command_resume(args: argparse.Namespace) -> int:
    return _apply_command(args, resume=True)


def command_status(args: argparse.Namespace) -> int:
    def run(engine: DriftEngine) -> tuple[int, Any]:
        return 0, engine.status()

    code, output = _with_engine(args, run)
    _emit(output)
    return code


def command_verify(args: argparse.Namespace) -> int:
    def run(engine: DriftEngine) -> tuple[int, Any]:
        result = engine.verify()
        return (0 if result["ok"] else 1), result

    code, output = _with_engine(args, run)
    _emit(output)
    return code


COMMANDS = {
    "load": command_load,
    "diff": command_diff,
    "plan": command_plan,
    "apply": command_apply,
    "resume": command_resume,
    "status": command_status,
    "verify": command_verify,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return COMMANDS[args.command](args)
    except EngineError as exc:
        _emit(exc.to_dict())
        return exc.exit_code
    except (sqlite3.Error, OSError, ValueError) as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E_RUNTIME",
                    "message": str(exc),
                    "details": {},
                },
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

