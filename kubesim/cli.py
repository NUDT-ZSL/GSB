from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .errors import EngineError
from .jsonutil import pretty_json, read_json
from .models import Dataset, parse_dataset
from .scheduler import Scheduler
from .store import Store


def _add_sample_arguments(parser: argparse.ArgumentParser, *, required: bool) -> None:
    parser.add_argument("--nodes", required=required)
    parser.add_argument("--pods", required=required)
    parser.add_argument("--config", dest="config_path", required=required)
    parser.add_argument("--injections")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kubesim",
        description="Deterministic local Kubernetes scheduling simulator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load a deterministic sample")
    load.add_argument("--db", required=True)
    _add_sample_arguments(load, required=True)

    schedule = subparsers.add_parser("schedule", help="execute or idempotently replay a round")
    schedule.add_argument("--db", required=True)
    _add_sample_arguments(schedule, required=False)
    schedule.add_argument("--round-id")
    schedule.add_argument("--instance-id", default="local")
    schedule.add_argument("--output", help="write the deterministic report to this path")

    resume = subparsers.add_parser("resume", help="resume an interrupted running round")
    resume.add_argument("--db", required=True)
    resume.add_argument("--round-id")
    resume.add_argument("--instance-id", default="resume")
    resume.add_argument("--output")

    explain = subparsers.add_parser("explain", help="explain one pod decision")
    explain.add_argument("--db", required=True)
    explain.add_argument("--pod", required=True)
    explain.add_argument("--round-id")

    status = subparsers.add_parser("status", help="show the latest committed snapshot")
    status.add_argument("--db", required=True)

    verify = subparsers.add_parser("verify", help="verify persisted state and report")
    verify.add_argument("--db", required=True)
    verify.add_argument("--report")
    return parser


def _dataset_from_paths(
    nodes_path: str,
    pods_path: str,
    config_path: str,
    injections_path: str | None,
) -> Dataset:
    nodes = read_json(nodes_path)
    pods = read_json(pods_path)
    config = read_json(config_path)
    injections = read_json(injections_path) if injections_path else None
    return parse_dataset(nodes, pods, config, injections)


def _schedule_files(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Dataset | None:
    supplied = [args.nodes, args.pods, args.config_path]
    if any(supplied) and not all(supplied):
        parser.error("schedule requires all of --nodes, --pods, and --config when one is supplied")
    if not all(supplied):
        if args.injections:
            parser.error("--injections requires node, pod, and config files")
        return None
    return _dataset_from_paths(
        args.nodes,
        args.pods,
        args.config_path,
        args.injections,
    )


def _write_report(report: dict[str, Any], output: str | None) -> None:
    text = pretty_json(report)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _success(payload: dict[str, Any]) -> int:
    sys.stdout.write(pretty_json(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            dataset = _dataset_from_paths(
                args.nodes,
                args.pods,
                args.config_path,
                args.injections,
            )
            store = Store(args.db)
            result = store.import_dataset(dataset)
            return _success(
                {
                    "ok": True,
                    "command": "load",
                    "database": str(Path(args.db)),
                    **result,
                }
            )

        if args.command == "schedule":
            supplied_dataset = _schedule_files(parser, args)
            store = Store(args.db)
            if supplied_dataset is not None:
                store.import_dataset(supplied_dataset)
                dataset = supplied_dataset
            else:
                dataset = store.latest_dataset()
            report = Scheduler(
                store,
                dataset,
                instance_id=args.instance_id,
            ).schedule(round_id=args.round_id)
            _write_report(report, args.output)
            return 0

        if args.command == "resume":
            store = Store(args.db)
            if args.round_id:
                round_row = store.get_round(args.round_id)
                if round_row is None:
                    raise EngineError(
                        "E_NOT_FOUND",
                        "round does not exist",
                        {"round_id": args.round_id},
                    )
            else:
                running = store.list_running_rounds()
                if not running:
                    report = store.latest_committed_report()
                    if report is None:
                        raise EngineError("E_NOT_FOUND", "no round is available to resume")
                    _write_report(report, args.output)
                    return 0
                round_row = running[0]
            dataset = store.get_dataset(round_row["batch_digest"])
            report = Scheduler(
                store,
                dataset,
                instance_id=args.instance_id,
            ).schedule(round_id=round_row["round_id"], resume=True)
            _write_report(report, args.output)
            return 0

        if args.command == "explain":
            store = Store(args.db)
            return _success(
                {
                    "ok": True,
                    "command": "explain",
                    **store.explain(args.pod, args.round_id),
                }
            )

        if args.command == "status":
            store = Store(args.db)
            return _success(
                {
                    "ok": True,
                    "command": "status",
                    **store.status(),
                }
            )

        if args.command == "verify":
            store = Store(args.db)
            result = store.verify(args.report)
            output = {"command": "verify", **result}
            sys.stdout.write(pretty_json(output))
            return 0 if result["ok"] else 1
    except EngineError as exc:
        sys.stderr.write(
            pretty_json(
                {
                    "ok": False,
                    "error": exc.to_dict(),
                }
            )
        )
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
