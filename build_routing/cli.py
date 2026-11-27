from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .diff import compare_projects, load_known
from .generator import DEFAULT_SEED, generate_artifacts
from .normalize import (
    ReferenceValidationError,
    load_project,
    reference_report,
)
from .store import WorkbenchStore


def _write_json(path: str | Path | None, value: Any) -> None:
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_generate(args: argparse.Namespace) -> int:
    result = generate_artifacts(args.out, seed=args.seed)
    _print_json(result["manifest"])
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        project = load_project(args.project, strict=args.strict)
    except ReferenceValidationError as exc:
        report = exc.report
    else:
        report = reference_report(project)
    _write_json(args.out, report)
    _print_json(report)
    return 0 if report["status"] == "ok" else 2


def _cmd_diff(args: argparse.Namespace) -> int:
    left = load_project(args.left)
    right = load_project(args.right)
    known = load_known(args.known) if args.known else None
    result = compare_projects(left, right, known)
    _write_json(args.out, result)
    _print_json(result["summary"])
    if known is not None and not result["known_validation"]["match"]:
        return 2
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    project = load_project(args.project, strict=args.strict_references)
    with Path(args.events).open("r", encoding="utf-8") as handle:
        events = json.load(handle)
    store = WorkbenchStore(args.db)
    result = store.bootstrap(
        project,
        events,
        reset=args.reset,
        max_new_events=args.max_events,
    )
    report = {
        "resumed_from_event_seq": result["resumed_from_event_seq"],
        "processed_event_count": result["processed_event_count"],
        "last_event_seq": result["last_event_seq"],
        "converged_at": result["converged_at"],
        "event_reports": result["event_reports"],
        "current": result["current"],
        "database": store.metadata(),
        "integrity": store.integrity_check(),
    }
    _write_json(args.report, report)
    if args.reference_report:
        _write_json(args.reference_report, reference_report(project))
    _print_json(
        {
            "resumed_from_event_seq": report["resumed_from_event_seq"],
            "processed_event_count": report["processed_event_count"],
            "last_event_seq": report["last_event_seq"],
            "converged_at": report["converged_at"],
            "database": report["database"]["counts"],
            "integrity": report["integrity"],
        }
    )
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    generated = generate_artifacts(args.out, seed=args.seed)
    project_v1 = load_project(Path(args.out) / "project_v1.json")
    project_v2 = load_project(Path(args.out) / "project_v2.json")
    with (Path(args.out) / "events.json").open("r", encoding="utf-8") as handle:
        events = json.load(handle)
    diff_report = compare_projects(
        project_v1,
        project_v2,
        load_known(Path(args.out) / "known_differences.json"),
    )
    _write_json(Path(args.out) / "diff_report.json", diff_report)
    _write_json(
        Path(args.out) / "reference_report.json",
        reference_report(project_v1),
    )
    store = WorkbenchStore(args.db)
    replay = store.bootstrap(project_v1, events, reset=not args.resume)
    replay_report = {
        "resumed_from_event_seq": replay["resumed_from_event_seq"],
        "processed_event_count": replay["processed_event_count"],
        "last_event_seq": replay["last_event_seq"],
        "converged_at": replay["converged_at"],
        "event_reports": replay["event_reports"],
        "current": replay["current"],
        "database": store.metadata(),
        "integrity": store.integrity_check(),
    }
    _write_json(Path(args.out) / "replay_report.json", replay_report)
    _print_json(
        {
            "seed": args.seed,
            "artifacts": generated["paths"],
            "database": args.db,
            "known_diff_match": diff_report["summary"]["known_match"],
            "last_event_seq": replay["last_event_seq"],
            "converged_at": replay["converged_at"],
            "switch_count": sum(
                item["switch_count"]
                for item in replay["current"]["prefixes"].values()
            ),
        }
    )
    return 0 if diff_report["summary"]["known_match"] else 2


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

    return run_server(
        db_path=args.db,
        project_path=args.project,
        events_path=args.events,
        host=args.host,
        port=args.port,
        reset=args.reset,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m build_routing",
        description="Deterministic build egress routing diagnostics",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic artifacts")
    generate.add_argument("--out", default="artifacts")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.set_defaults(func=_cmd_generate)

    validate = sub.add_parser("validate", help="validate policy references")
    validate.add_argument("--project", required=True)
    validate.add_argument("--out")
    validate.add_argument("--strict", action="store_true")
    validate.set_defaults(func=_cmd_validate)

    diff = sub.add_parser("diff", help="compare normalized v1 and v2 projects")
    diff.add_argument("--left", required=True)
    diff.add_argument("--right", required=True)
    diff.add_argument("--known")
    diff.add_argument("--out")
    diff.set_defaults(func=_cmd_diff)

    replay = sub.add_parser("replay", help="initialize or resume SQLite replay")
    replay.add_argument("--project", required=True)
    replay.add_argument("--events", required=True)
    replay.add_argument("--db", required=True)
    replay.add_argument("--reset", action="store_true")
    replay.add_argument("--max-events", type=int)
    replay.add_argument("--report")
    replay.add_argument("--reference-report")
    replay.add_argument("--strict-references", action="store_true")
    replay.set_defaults(func=_cmd_replay)

    demo = sub.add_parser(
        "demo", help="generate artifacts, compare versions, and replay into SQLite"
    )
    demo.add_argument("--out", default="artifacts")
    demo.add_argument("--db", default="artifacts/workbench.db")
    demo.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo.add_argument("--resume", action="store_true")
    demo.set_defaults(func=_cmd_demo)

    serve = sub.add_parser("serve", help="run the local HTTP workbench")
    serve.add_argument("--db", default="artifacts/workbench.db")
    serve.add_argument("--project")
    serve.add_argument("--events")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reset", action="store_true")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
