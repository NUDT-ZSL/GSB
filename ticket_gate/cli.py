from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .compare import compare_documents, load_json
from .engine import (
    EngineError,
    build_report,
    decision_stats,
    prepare_database,
    propagate,
    verify_requests,
    write_report_files,
)
from .generator import generate_project
from .graph import build_plan
from .parser import load_project


REFERENCE_SCRIPT = Path(__file__).resolve().parent.parent / "reference_judge.py"


def _print_json(document: Any) -> None:
    print(json.dumps(document, ensure_ascii=False, indent=2))


def _write_json(path: str | Path, document: Any) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return output


def _load_project_or_fail(path: str):
    project = load_project(path)
    if not project.valid:
        _print_json(
            {
                "ok": False,
                "project_root": project.root,
                "issues": [issue.to_dict() for issue in project.issues],
            }
        )
        raise SystemExit(2)
    return project


def _run_reference(project_path: str, reference_path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            sys.executable,
            str(REFERENCE_SCRIPT),
            "--project",
            str(Path(project_path).resolve()),
            "--out",
            str(reference_path),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise EngineError(
            "independent reference judge failed: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    return load_json(reference_path)


def cmd_generate(args: argparse.Namespace) -> int:
    root = generate_project(args.out, args.seed, args.overwrite)
    project = load_project(root)
    _print_json(
        {
            "ok": project.valid,
            "project_root": str(root),
            **project.to_summary(),
        }
    )
    return 0 if project.valid else 2


def cmd_validate(args: argparse.Namespace) -> int:
    project = load_project(args.project)
    _print_json(
        {
            "ok": project.valid,
            "project_root": project.root,
            **project.to_summary(),
        }
    )
    return 0 if project.valid else 2


def cmd_plan(args: argparse.Namespace) -> int:
    project = _load_project_or_fail(args.project)
    plan = build_plan(project)
    document = plan.to_dict()
    if args.db:
        prepare_database(project, args.db, reset=args.reset)
        document["database"] = str(Path(args.db).resolve())
    if args.out:
        _write_json(args.out, document)
    _print_json(
        {
            "ok": True,
            "affected_gate_count": len(plan.affected_gate_ids),
            "batch_count": len(plan.batches),
            "cycle_count": len(plan.cycles),
            "conflict_gate_count": len({item.gate_id for item in plan.conflicts}),
            "output": str(Path(args.out).resolve()) if args.out else None,
        }
    )
    return 0


def cmd_propagate(args: argparse.Namespace) -> int:
    project = _load_project_or_fail(args.project)
    if args.reset:
        prepare_database(project, args.db, reset=True)
    result = propagate(
        project,
        args.db,
        crash_after_batch=args.crash_after_batch,
    )
    _print_json(result)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    project = _load_project_or_fail(args.project)
    prepare_database(project, args.db)
    decisions = verify_requests(
        project,
        args.db,
        allow_incomplete=args.allow_incomplete,
    )
    stats = decision_stats(decisions)
    _print_json(
        {
            "ok": True,
            "decisions": len(decisions),
            "stats": stats,
            "degraded_decisions": stats["degraded_decisions"],
        }
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    project = _load_project_or_fail(args.project)
    prepare_database(project, args.db)
    report = build_report(project, args.db)
    write_report_files(report, args.out)
    _print_json(
        {
            "ok": True,
            "output": str(Path(args.out).resolve()),
            "decision_stats": report["verification"]["stats"],
            "invariants": report["invariants"],
        }
    )
    return 0 if all(report["invariants"].values()) else 1


def cmd_reference(args: argparse.Namespace) -> int:
    reference = _run_reference(args.project, Path(args.out))
    _print_json(
        {
            "ok": True,
            "output": str(Path(args.out).resolve()),
            "affected_gates": len(reference["affected_gate_ids"]),
            "batches": len(reference["batches"]),
            "decisions": len(reference["decisions"]),
            "degraded_gates": len(reference["degraded_gates"]),
        }
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    kernel_path = Path(args.kernel)
    if kernel_path.is_dir():
        kernel_path = kernel_path / "summary.json"
    result = compare_documents(load_json(kernel_path), load_json(args.reference))
    if args.out:
        _write_json(args.out, result)
    _print_json(result)
    return 0 if result["ok"] else 1


def cmd_run(args: argparse.Namespace) -> int:
    project = _load_project_or_fail(args.project)
    if args.reset:
        prepare_database(project, args.db, reset=True)
    propagate(
        project,
        args.db,
        crash_after_batch=args.crash_after_batch,
    )
    verify_requests(project, args.db)
    report = build_report(project, args.db)
    write_report_files(report, args.out)

    reference_path = Path(args.reference_out)
    reference = _run_reference(args.project, reference_path)
    comparison = compare_documents(report, reference)
    compare_path = Path(args.out).resolve() / "compare.json"
    _write_json(compare_path, comparison)
    _print_json(
        {
            "ok": comparison["ok"] and all(report["invariants"].values()),
            "database": str(Path(args.db).resolve()),
            "kernel_output": str(Path(args.out).resolve()),
            "reference_output": str(reference_path.resolve()),
            "comparison_output": str(compare_path),
            "decision_stats": report["verification"]["stats"],
            "degraded_gate_ids": report["degradation"]["gate_ids"],
            "invariants": report["invariants"],
            "reference_comparison_ok": comparison["ok"],
        }
    )
    return 0 if comparison["ok"] and all(report["invariants"].values()) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket-gate",
        description="Deterministic ticket-rule propagation and verification kernel",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a seeded project")
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--out", required=True)
    generate.add_argument("--overwrite", action="store_true")
    generate.set_defaults(handler=cmd_generate)

    validate = subparsers.add_parser("validate", help="validate and normalize a project")
    validate.add_argument("--project", required=True)
    validate.set_defaults(handler=cmd_validate)

    plan = subparsers.add_parser("plan", help="compute affected gates and dependency batches")
    plan.add_argument("--project", required=True)
    plan.add_argument("--db")
    plan.add_argument("--reset", action="store_true")
    plan.add_argument("--out")
    plan.set_defaults(handler=cmd_plan)

    propagate_parser = subparsers.add_parser(
        "propagate", help="execute or resume propagation"
    )
    propagate_parser.add_argument("--project", required=True)
    propagate_parser.add_argument("--db", required=True)
    propagate_parser.add_argument(
        "--crash-after-batch",
        type=int,
        help="commit this batch, then terminate the process with exit code 75",
    )
    propagate_parser.add_argument("--reset", action="store_true")
    propagate_parser.set_defaults(handler=cmd_propagate)

    verify = subparsers.add_parser("verify", help="write verification decisions")
    verify.add_argument("--project", required=True)
    verify.add_argument("--db", required=True)
    verify.add_argument("--allow-incomplete", action="store_true")
    verify.set_defaults(handler=cmd_verify)

    report = subparsers.add_parser("report", help="write the complete auditable report")
    report.add_argument("--project", required=True)
    report.add_argument("--db", required=True)
    report.add_argument("--out", required=True)
    report.set_defaults(handler=cmd_report)

    reference = subparsers.add_parser(
        "reference", help="run the independent reference judge"
    )
    reference.add_argument("--project", required=True)
    reference.add_argument("--out", required=True)
    reference.set_defaults(handler=cmd_reference)

    compare = subparsers.add_parser("compare", help="compare kernel and reference outputs")
    compare.add_argument("--kernel", required=True)
    compare.add_argument("--reference", required=True)
    compare.add_argument("--out")
    compare.set_defaults(handler=cmd_compare)

    run = subparsers.add_parser(
        "run", help="propagate, verify, report, run reference, and compare"
    )
    run.add_argument("--project", default="sample_project")
    run.add_argument("--db", default="runtime/run.db")
    run.add_argument("--out", default="runtime/kernel")
    run.add_argument("--reference-out", default="runtime/reference/reference.json")
    run.add_argument(
        "--crash-after-batch",
        type=int,
        help="commit this batch, then terminate with exit code 75",
    )
    run.add_argument("--reset", action="store_true")
    run.set_defaults(handler=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (EngineError, FileExistsError, ValueError) as exc:
        _print_json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
