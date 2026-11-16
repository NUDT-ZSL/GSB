"""Command-line interface for local Windows-friendly experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .anomalies import run_anomaly
from .engine import Engine
from .errors import E_PARSE, LabError
from .util import parse_null_overrides


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE = ROOT / "samples" / "demo.sql"
DEFAULT_QUERY = ROOT / "samples" / "query.sql"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _query_text(args: argparse.Namespace) -> str:
    if getattr(args, "query", None):
        return args.query
    path = Path(getattr(args, "query_file", None) or DEFAULT_QUERY)
    return path.read_text(encoding="utf-8")


def _add_query_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", help="SQL SELECT text")
    parser.add_argument("--query-file", help="path to a SQL SELECT file")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sqlplan_lab",
        description="Local SQL logical-plan attribution and deviation heat-lab",
    )
    parser.add_argument("--db", default="lab.sqlite3", help="SQLite database file")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="atomically ingest a local SQL sample")
    ingest.add_argument("--sql", default=str(DEFAULT_SAMPLE), help="SQL sample file")

    stats = sub.add_parser("stats", help="create a statistics snapshot")
    stats.add_argument("--buckets", type=int, default=8)
    stats.add_argument("--sample-rate", type=float, default=1.0)
    stats.add_argument(
        "--null-override",
        action="append",
        default=[],
        metavar="TABLE.COLUMN=FRACTION",
    )

    plan = sub.add_parser("plan", help="parse and print the stable logical plan")
    _add_query_options(plan)

    estimate = sub.add_parser("estimate", help="estimate every logical operator")
    _add_query_options(estimate)
    estimate.add_argument("--stats", help="statistics version; defaults to session version")

    analyze = sub.add_parser("analyze", help="estimate, measure, attribute and refresh heatmap")
    _add_query_options(analyze)
    analyze.add_argument("--stats", help="statistics version; defaults to session version")

    compare = sub.add_parser("compare", help="compare two statistics versions")
    _add_query_options(compare)
    compare.add_argument("--old-stats", required=True)
    compare.add_argument("--new-stats", required=True)

    snapshots = sub.add_parser("snapshots", help="list snapshots")
    snapshots.set_defaults(command="snapshots")

    save = sub.add_parser("snapshot-save", help="save the complete session snapshot")
    save.add_argument("name")

    restore = sub.add_parser("snapshot-restore", help="whole-state replacement from a snapshot")
    restore.add_argument("name")

    verify = sub.add_parser("snapshot-verify", help="replay all plans in the restored session")
    verify.set_defaults(command="snapshot-verify")

    anomaly = sub.add_parser("anomaly", help="inject one reproducible business anomaly")
    anomaly.add_argument("type", choices=["skew", "stats", "replay"])
    _add_query_options(anomaly)
    anomaly.add_argument("--stats", help="statistics version for the stats anomaly")

    heatmap = sub.add_parser("heatmap", help="show the current complete heatmap")
    heatmap.set_defaults(command="heatmap")

    logs = sub.add_parser("logs", help="show stable structured anomaly logs")
    logs.set_defaults(command="logs")

    gui = sub.add_parser("gui", help="launch the Tkinter interface")
    gui.set_defaults(command="gui")
    return parser


def execute(args: argparse.Namespace) -> Any:
    db_path = Path(args.db)
    if args.command == "gui":
        from .gui import launch_gui

        return launch_gui(db_path)
    with Engine(db_path) as engine:
        if args.command == "ingest":
            return engine.ingest_sql_file(args.sql)
        if args.command == "stats":
            overrides = parse_null_overrides(args.null_override)
            return engine.create_stats_version(
                args.buckets, args.sample_rate, overrides
            )
        if args.command == "plan":
            query = _query_text(args)
            plan = engine.build_plan(query)
            return {
                "plan_id": plan["plan_id"],
                "normalized_sql": plan["normalized_sql"],
                "tree": plan["tree"],
                "tree_text": "\n".join(plan["tree"]),
                "topological_order": plan["topological_order"],
            }
        if args.command == "estimate":
            return engine.estimate(_query_text(args), args.stats)
        if args.command == "analyze":
            return engine.analyze(_query_text(args), args.stats)
        if args.command == "compare":
            return engine.compare_stats_versions(
                _query_text(args), args.old_stats, args.new_stats
            )
        if args.command == "snapshots":
            return {"snapshots": engine.list_snapshots()}
        if args.command == "snapshot-save":
            return engine.save_snapshot(args.name)
        if args.command == "snapshot-restore":
            return engine.restore_snapshot(args.name)
        if args.command == "snapshot-verify":
            return {"history": engine.verify_restored_history()}
        if args.command == "anomaly":
            return run_anomaly(
                engine,
                args.type,
                query_text=_query_text(args) if args.type == "replay" else None,
                stats_version=args.stats,
            )
        if args.command == "heatmap":
            return {"rows": engine.load_heatmap()}
        if args.command == "logs":
            return {"logs": engine.list_logs()}
    raise LabError(E_PARSE, f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if args.command == "gui":
            return int(result or 0)
        print(_json(result))
        return 0
    except LabError as exc:
        print(_json(exc.to_dict()))
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(_json({"ok": False, "code": "E_INTERNAL", "message": str(exc)}))
        return 3
