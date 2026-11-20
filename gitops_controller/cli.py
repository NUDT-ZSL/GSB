"""Command-line interface for the local GitOps controller."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .controller import Controller
from .engine import resume as resume_engine
from .engine import rollback as rollback_engine
from .errors import GitOpsError
from .project import Project, default_project_path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        default=argparse.SUPPRESS,
        help="path to the project JSON file",
    )
    parser.add_argument(
        "--clock-ms",
        type=int,
        default=argparse.SUPPRESS,
        help="injected logical clock in Unix milliseconds",
    )
    parser.add_argument(
        "--tolerance-ms",
        type=int,
        default=argparse.SUPPRESS,
        help="logical clock rollback tolerance",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS,
        help="emit compact sorted JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gitops-controller",
        description="Deterministic local GitOps rollback and convergence controller",
    )
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="load, validate, and merge manifests")
    _add_common(load_parser)

    plan_parser = subparsers.add_parser("plan", help="generate a rollback plan")
    _add_common(plan_parser)
    plan_parser.add_argument("--env", required=True)

    rollback_parser = subparsers.add_parser("rollback", help="execute a rollback")
    _add_common(rollback_parser)
    rollback_parser.add_argument("--env", required=True)
    rollback_parser.add_argument("--budget-ms", type=int)
    rollback_parser.add_argument("--operation-cost-ms", type=int)
    rollback_parser.add_argument(
        "--crash-after",
        type=int,
        help="test hook: _exit after N cluster writes before commit",
    )
    rollback_parser.add_argument("--fail-app", action="append", default=[])
    rollback_parser.add_argument("--abandon-app", action="append", default=[])

    resume_parser = subparsers.add_parser("resume", help="resume an interrupted rollback")
    _add_common(resume_parser)
    resume_parser.add_argument("--env", required=True)
    resume_parser.add_argument("--budget-ms", type=int)
    resume_parser.add_argument("--operation-cost-ms", type=int)
    resume_parser.add_argument("--crash-after", type=int)
    resume_parser.add_argument("--fail-app", action="append", default=[])
    resume_parser.add_argument("--abandon-app", action="append", default=[])

    status_parser = subparsers.add_parser("status", help="show convergence status")
    _add_common(status_parser)
    status_parser.add_argument("--env", action="append", default=[])

    verify_parser = subparsers.add_parser(
        "verify",
        help="verify persisted state against recomputed manifests and the cluster",
    )
    _add_common(verify_parser)
    verify_parser.add_argument("--env", action="append", default=[])
    return parser


def _emit(value: Any, *, compact: bool) -> None:
    if compact:
        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    sys.stdout.write(text + "\n")


def _selected_environments(project: Project, requested: list[str]) -> list[str]:
    if not requested:
        return list(project.environments)
    unknown = sorted(set(requested) - set(project.environments))
    if unknown:
        raise GitOpsError(
            "E_BAD_CONFIG",
            "unknown environment",
            {"known": list(project.environments), "unknown": unknown},
        )
    return list(requested)


def _error_payload(error: GitOpsError) -> dict[str, Any]:
    return {
        "error": error.to_dict(),
        "format": "error-v1",
        "kind": "error",
        "ok": False,
        "schema_version": 1,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_path = Path(getattr(args, "project", default_project_path()))
    clock_ms = getattr(args, "clock_ms", None)
    tolerance_ms = getattr(args, "tolerance_ms", None)
    compact = bool(getattr(args, "compact", False))
    try:
        if tolerance_ms is not None and tolerance_ms < 0:
            raise GitOpsError(
                "E_BAD_CONFIG",
                "clock tolerance must be non-negative",
                {"tolerance_ms": tolerance_ms},
            )
        project = Project.load(project_path)
        controller = Controller(project)

        if args.command == "load":
            result = controller.load(
                clock_ms=clock_ms,
                tolerance_ms=tolerance_ms,
            )
            _emit(result, compact=compact)
            return 0

        if args.command == "plan":
            controller.load(clock_ms=clock_ms, tolerance_ms=tolerance_ms)
            from .planning import plan_environment

            environment = _selected_environments(project, [args.env])[0]
            result = plan_environment(project, controller.store, environment)
            result = {
                **result,
                "ok": True,
                "schema_version": 1,
            }
            _emit(result, compact=compact)
            return 0

        if args.command in ("rollback", "resume"):
            budget_ms = (
                project.default_budget_ms
                if args.budget_ms is None
                else args.budget_ms
            )
            operation_cost_ms = (
                project.operation_cost_ms
                if args.operation_cost_ms is None
                else args.operation_cost_ms
            )
            if args.command == "rollback" and budget_ms < 0:
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    "budget must be non-negative",
                    {"budget_ms": budget_ms},
                )
            if operation_cost_ms <= 0:
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    "operation cost must be positive",
                    {"operation_cost_ms": operation_cost_ms},
                )
            if args.crash_after is not None and args.crash_after <= 0:
                raise GitOpsError(
                    "E_BAD_CONFIG",
                    "crash-after must be positive",
                    {"crash_after": args.crash_after},
                )
            environment = _selected_environments(project, [args.env])[0]
            if args.command == "rollback":
                result = rollback_engine(
                    controller,
                    environment,
                    budget_ms=budget_ms,
                    operation_cost_ms=operation_cost_ms,
                    clock_ms=clock_ms,
                    tolerance_ms=tolerance_ms,
                    crash_after=args.crash_after,
                    fail_apps=set(args.fail_app),
                    abandon_apps=set(args.abandon_app),
                )
            else:
                result = resume_engine(
                    controller,
                    environment,
                    budget_ms=None if args.budget_ms is None else budget_ms,
                    operation_cost_ms=(
                        None if args.operation_cost_ms is None else operation_cost_ms
                    ),
                    clock_ms=clock_ms,
                    tolerance_ms=tolerance_ms,
                    crash_after=args.crash_after,
                    fail_apps=set(args.fail_app),
                    abandon_apps=set(args.abandon_app),
                )
            _emit(result, compact=compact)
            return 0

        if args.command == "status":
            controller.load(clock_ms=clock_ms, tolerance_ms=tolerance_ms)
            environments = _selected_environments(project, args.env)
            result = controller.status(environments)
            _emit(result, compact=compact)
            return 0

        if args.command == "verify":
            controller.load(clock_ms=clock_ms, tolerance_ms=tolerance_ms)
            environments = _selected_environments(project, args.env)
            result = controller.verify(environments)
            _emit(result, compact=compact)
            return 0 if result["ok"] else 1

        raise GitOpsError(
            "E_BAD_CONFIG",
            f"unknown command: {args.command}",
            {"command": args.command},
        )
    except GitOpsError as error:
        _emit(_error_payload(error), compact=compact)
        return 2
    except (OSError, sqlite3.Error) as error:
        wrapped = GitOpsError(
            "E_IO",
            "local file or SQLite operation failed",
            {"reason": str(error)},
        )
        _emit(_error_payload(wrapped), compact=compact)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

