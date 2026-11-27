from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .evaluator import evaluate_project
from .errors import ProjectValidationError
from .generator import build_project
from .incremental import (
    compare_evaluations,
    incremental_evaluate,
    load_state,
    write_state,
)
from .loader import load_project, validate_project_data
from .snapshot import (
    list_snapshots,
    load_snapshot_state,
    query_snapshot,
    save_snapshot,
)
from .utils import canonical_json, write_json_atomic


def _emit(value: Any, output: str | None) -> None:
    text = canonical_json(value)
    if output is None or output == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    write_json_atomic(output, value)
    summary = {
        "output": str(Path(output)),
        "project_id": value.get("project_id") if isinstance(value, dict) else None,
    }
    sys.stdout.write(canonical_json(summary))
    sys.stdout.flush()


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output",
        "-o",
        help="write JSON to this path instead of stdout",
    )


def _parse_range(value: str, option: str) -> tuple[int, int]:
    parts = value.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"{option} must use START:END inclusive syntax")
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError as error:
        raise ValueError(f"{option} bounds must be integers") from error
    if start < 0 or end < start:
        raise ValueError(f"{option} requires 0 <= START <= END")
    return start, end


def _cmd_generate(args: argparse.Namespace) -> int:
    project_data = build_project(seed=args.seed, scenario=args.scenario)
    if args.output is None or args.output == "-":
        sys.stdout.write(canonical_json(project_data))
        return 0
    target = Path(args.output)
    if target.exists() and not args.force:
        raise ValueError(
            f"output already exists: {target}; use --force to overwrite"
        )
    write_json_atomic(target, project_data)
    issues, project = validate_project_data(project_data)
    if issues or project is None:
        raise ProjectValidationError(issues)
    sys.stdout.write(
        canonical_json(
            {
                "generated": str(target),
                "project_id": project.project_id,
                "project_fingerprint": project.fingerprint,
                "seed": args.seed,
                "scenario": args.scenario,
            }
        )
    )
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        project = load_project(args.project)
    except ProjectValidationError as error:
        _emit(error.as_dict(), args.output)
        return 1
    _emit(
        {
            "valid": True,
            "project_id": project.project_id,
            "project_fingerprint": project.fingerprint,
            "source_path": project.source_path,
            "issues": [],
        },
        args.output,
    )
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    _emit(evaluate_project(project), args.output)
    return 0


def _cmd_incremental(args: argparse.Namespace) -> int:
    base_project = load_project(args.base)
    changed_project = load_project(args.changed)
    if args.restore_snapshot and args.state:
        raise ValueError(
            "--state and --restore-snapshot are mutually exclusive"
        )
    baseline_evaluation = None
    baseline_source: dict[str, Any] = {"type": "full_base_evaluation"}
    if args.restore_snapshot:
        if not args.snapshot_version:
            raise ValueError(
                "--restore-snapshot requires --snapshot-version"
            )
        snapshot_project, snapshot_evaluation = load_snapshot_state(
            args.restore_snapshot,
            base_project.project_id,
            args.snapshot_version,
        )
        if snapshot_project.fingerprint != base_project.fingerprint:
            raise ValueError(
                "snapshot project does not match the supplied base project"
            )
        baseline_evaluation = snapshot_evaluation
        baseline_source = {
            "type": "snapshot_reload",
            "store": str(Path(args.restore_snapshot)),
            "version": args.snapshot_version,
        }
    elif args.state:
        state_project, state_evaluation = load_state(args.state)
        if state_project.fingerprint != base_project.fingerprint:
            raise ValueError(
                "state project does not match the supplied base project"
            )
        baseline_evaluation = state_evaluation
        baseline_source = {
            "type": "state_reload",
            "path": str(Path(args.state)),
        }

    result = incremental_evaluate(
        base_project,
        changed_project,
        baseline_evaluation=baseline_evaluation,
        verify_full=not args.no_verify,
    )
    result["baseline_source"] = baseline_source
    if args.write_state:
        write_state(
            args.write_state,
            changed_project,
            result["evaluation"],
        )
        result["state_written"] = str(Path(args.write_state))
    _emit(result, args.output)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    left_project = load_project(args.left)
    right_project = load_project(args.right)
    left_evaluation = evaluate_project(left_project)
    right_evaluation = evaluate_project(right_project)
    consistency = compare_evaluations(left_evaluation, right_evaluation)
    _emit(
        {
            "comparison_schema": 1,
            "left_project_id": left_project.project_id,
            "right_project_id": right_project.project_id,
            "left_project_fingerprint": left_project.fingerprint,
            "right_project_fingerprint": right_project.fingerprint,
            "consistency": consistency,
        },
        args.output,
    )
    return 0


def _cmd_snapshot_save(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    evaluation = evaluate_project(project)
    target = save_snapshot(
        args.store,
        project,
        args.version,
        evaluation=evaluation,
    )
    _emit(
        {
            "saved": True,
            "path": str(target),
            "project_id": project.project_id,
            "version": args.version,
            "project_fingerprint": project.fingerprint,
            "summary": evaluation["summary"],
            "alignment": evaluation["alignment"],
        },
        args.output,
    )
    return 0


def _cmd_snapshot_query(args: argparse.Namespace) -> int:
    row_range = (
        _parse_range(args.rows, "--rows")
        if args.rows is not None
        else None
    )
    col_range = (
        _parse_range(args.cols, "--cols")
        if args.cols is not None
        else None
    )
    result = query_snapshot(
        args.store,
        args.project_id,
        args.version,
        pose_ids=args.pose or None,
        row_range=row_range,
        col_range=col_range,
    )
    _emit(result, args.output)
    return 0


def _cmd_snapshot_list(args: argparse.Namespace) -> int:
    _emit(list_snapshots(args.store, args.project_id), args.output)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livingroom-stereo",
        description=(
            "Deterministic living-room stereo display assessment kernel"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate the deterministic sample project"
    )
    generate.add_argument("--seed", type=int, default=20261128)
    generate.add_argument(
        "--scenario",
        choices=("base", "adjusted"),
        default="base",
    )
    generate.add_argument("--force", action="store_true")
    _add_output(generate)
    generate.set_defaults(handler=_cmd_generate)

    validate = subparsers.add_parser(
        "validate", help="validate a project and print a stable issue list"
    )
    validate.add_argument("project")
    _add_output(validate)
    validate.set_defaults(handler=_cmd_validate)

    evaluate = subparsers.add_parser(
        "evaluate", help="run a full assessment"
    )
    evaluate.add_argument("project")
    _add_output(evaluate)
    evaluate.set_defaults(handler=_cmd_evaluate)

    incremental = subparsers.add_parser(
        "incremental",
        help="reassess only dependency-affected blocks",
    )
    incremental.add_argument("base")
    incremental.add_argument("changed")
    incremental.add_argument(
        "--state",
        help="baseline state written by a previous run",
    )
    incremental.add_argument(
        "--write-state",
        help="persist the changed project and result for the next run",
    )
    incremental.add_argument(
        "--restore-snapshot",
        help="snapshot store used as the baseline",
    )
    incremental.add_argument("--snapshot-version")
    incremental.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the full-recalculation consistency comparison",
    )
    _add_output(incremental)
    incremental.set_defaults(handler=_cmd_incremental)

    compare = subparsers.add_parser(
        "compare",
        help="compare two full assessments block by block",
    )
    compare.add_argument("left")
    compare.add_argument("right")
    _add_output(compare)
    compare.set_defaults(handler=_cmd_compare)

    snapshot = subparsers.add_parser(
        "snapshot", help="save, query or list versioned snapshots"
    )
    snapshot_subparsers = snapshot.add_subparsers(
        dest="snapshot_command", required=True
    )
    snapshot_save = snapshot_subparsers.add_parser("save")
    snapshot_save.add_argument("project")
    snapshot_save.add_argument("--store", required=True)
    snapshot_save.add_argument("--version", required=True)
    _add_output(snapshot_save)
    snapshot_save.set_defaults(handler=_cmd_snapshot_save)

    snapshot_query = snapshot_subparsers.add_parser("query")
    snapshot_query.add_argument("--store", required=True)
    snapshot_query.add_argument("--project-id", required=True)
    snapshot_query.add_argument("--version", required=True)
    snapshot_query.add_argument(
        "--pose",
        action="append",
        help="pose id filter; repeat for multiple poses",
    )
    snapshot_query.add_argument(
        "--rows",
        help="inclusive block row range START:END",
    )
    snapshot_query.add_argument(
        "--cols",
        help="inclusive block column range START:END",
    )
    _add_output(snapshot_query)
    snapshot_query.set_defaults(handler=_cmd_snapshot_query)

    snapshot_list = snapshot_subparsers.add_parser("list")
    snapshot_list.add_argument("--store", required=True)
    snapshot_list.add_argument("--project-id", required=True)
    _add_output(snapshot_list)
    snapshot_list.set_defaults(handler=_cmd_snapshot_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        handler: Callable[[argparse.Namespace], int] = args.handler
        return handler(args)
    except ProjectValidationError as error:
        sys.stderr.write(canonical_json(error.as_dict()))
        return 2
    except (ValueError, OSError, json.JSONDecodeError) as error:
        sys.stderr.write(
            canonical_json(
                {
                    "valid": False,
                    "error": {
                        "code": "E_RUNTIME",
                        "message": str(error),
                    },
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
