"""Command-line diagnostic workbench."""

from __future__ import annotations

import argparse
import copy
import json
import os
import signal
from pathlib import Path
from typing import Any

from .cache import ContentCache
from .catalog import Catalog
from .checkpoint import CheckpointManager
from .config import EngineConfig, ResourceLimits
from .diagnostics import dashboard_text
from .engine import DiagnosticEngine
from .errors import BusinessError, format_error
from .generator import generate_dataset
from .replay import replay_task
from .revision import RevisionRequest, RevisionService
from .snapshots import load_snapshot, migrate_snapshot
from .tasks import parse_tasks


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    compact = copy.deepcopy(report)
    compact.pop("artifacts", None)
    return compact


def _limits(args: argparse.Namespace) -> ResourceLimits:
    values: dict[str, int] = {}
    for name in (
        "max_task_memory_bytes",
        "max_cache_entries",
        "max_open_shards",
        "max_recompute_tasks",
    ):
        value = getattr(args, name, None)
        if value is not None:
            values[name] = int(value)
    return ResourceLimits.from_mapping(values)


def _add_limit_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-task-memory-bytes", type=int, default=None)
    parser.add_argument("--max-cache-entries", type=int, default=None)
    parser.add_argument("--max-open-shards", type=int, default=None)
    parser.add_argument("--max-recompute-tasks", type=int, default=None)


def _add_engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--run-id", default="primary")
    parser.add_argument("--snapshot-format", choices=("v1", "v2"), default="v2")
    parser.add_argument("--algorithm-version", default="adam-peft-v1")
    _add_limit_arguments(parser)


def _engine_config(args: argparse.Namespace, *, run_id: str | None = None) -> EngineConfig:
    return EngineConfig(
        tasks_path=args.tasks,
        catalog_path=args.catalog,
        state_dir=args.state_dir,
        run_id=run_id or args.run_id,
        algorithm_version=getattr(args, "algorithm_version", "adam-peft-v1"),
        snapshot_format=getattr(args, "snapshot_format", "v2"),
        limits=_limits(args),
    )


def command_generate(args: argparse.Namespace) -> dict[str, Any]:
    return generate_dataset(args.output, seed=args.seed, task_count=args.task_count)


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    tasks = parse_tasks(args.tasks)
    result: dict[str, Any] = {
        "ok": True,
        "task_count": len(tasks),
        "tasks": [],
    }
    catalog = Catalog(args.catalog) if args.catalog else None
    for task in tasks:
        record: dict[str, Any] = {
            "case_id": task.case_id,
            "task_fingerprint": task.fingerprint,
            "parameter_summary": task.parameter_summary,
            "trainable_params": task.trainable_params,
            "optimizer_steps": task.optimizer_steps,
        }
        if catalog:
            formats = ("v1", "v2") if args.all_formats else (args.snapshot_format,)
            snapshots: dict[str, Any] = {}
            for format_version in formats:
                path = catalog.snapshot_path(task.case_id, format_version)
                snapshot = load_snapshot(path, task, limits=_limits(args))
                snapshots[format_version] = {
                    "path": path,
                    "snapshot_id": snapshot.snapshot_id,
                    "semantic_hash": snapshot.semantic_hash,
                    "generation": snapshot.generation,
                    "revision": snapshot.revision,
                }
            record["snapshots"] = snapshots
            if len(set(item["semantic_hash"] for item in snapshots.values())) != 1:
                raise format_error(
                    "v1/v2 snapshots are not semantically equal",
                    task_id=task.case_id,
                    field="semantic_hash",
                )
        result["tasks"].append(record)
    return result


def command_train(args: argparse.Namespace) -> dict[str, Any]:
    config = _engine_config(args)
    stop = {"requested": False}

    def handle_signal(signum: int, frame: Any) -> None:
        stop["requested"] = True

    previous_handlers: dict[int, Any] = {}
    if not args.no_signal_handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, handle_signal)
    kill_hook = None
    if args.simulate_kill_at:
        try:
            kill_stage, kill_cycle, kill_index = args.simulate_kill_at.split(":")
            kill_spec = (kill_stage, int(kill_cycle), int(kill_index))
        except Exception:
            raise format_error(
                "simulate-kill-at must be STAGE:CYCLE:TASK_INDEX",
                field="simulate_kill_at",
            )

        def kill_hook(stage: str, cycle: int, index: int, task_id: str) -> None:
            if (stage, cycle, index) == kill_spec:
                os._exit(137)

    try:
        report = DiagnosticEngine(config).run(
            recover=not args.no_recover,
            interrupt_check=lambda: stop["requested"],
            kill_hook=kill_hook,
        )
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return _public_report(report)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    manager = CheckpointManager(args.state_dir, args.run_id)
    checkpoint = manager.recover_latest()
    cache = ContentCache(
        Path(args.state_dir) / "cache",
        max_entries=args.max_cache_entries or 256,
    )
    report_path = Path(args.state_dir) / "reports" / f"{args.run_id}.json"
    report = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            report = None
    revision_history_path = Path(args.state_dir) / "revision-history.json"
    revision_history = []
    if revision_history_path.is_file():
        try:
            revision_history = json.loads(revision_history_path.read_text(encoding="utf-8"))
        except Exception:
            revision_history = []
    task_states = {}
    if checkpoint:
        for task_id, state in checkpoint.get("task_states", {}).items():
            task_states[task_id] = {
                "step": state.get("step"),
                "update_count": state.get("update_count"),
                "micro_in_window": state.get("micro_in_window"),
            }
    return {
        "ok": True,
        "run_id": args.run_id,
        "checkpoint": manager.summary(),
        "task_states_at_commit": task_states,
        "cache": cache.summary(),
        "report": report,
        "revision_history": revision_history,
        "state_machine": {
            "stages": ["INITIALIZE", "GRAD_ACCUM", "OPTIMIZER_UPDATE", "EXPORT"],
            "recovery_rule": (
                "resume from latest committed stage; discard all uncommitted task work "
                "and rebuild the interrupted stage from its start"
            ),
        },
    }


def command_dashboard(args: argparse.Namespace) -> dict[str, Any]:
    status = command_status(args)
    print(dashboard_text(status))
    if args.interactive:
        print("Interactive commands: status, tasks, cache, revisions, recover, help, quit")
        while True:
            try:
                command = input("peft-diag> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if command in {"quit", "exit", "q"}:
                break
            if command in {"help", "h"}:
                print("status | tasks | cache | revisions | recover | quit")
            elif command == "tasks":
                _print(status["task_states_at_commit"])
            elif command == "cache":
                _print(status["cache"])
            elif command == "revisions":
                _print(status["revision_history"])
            elif command == "recover":
                _print(status["checkpoint"])
            elif command in {"status", ""}:
                print(dashboard_text(command_status(args)))
            else:
                print("unknown command; type help")
    return {"ok": True, "dashboard": status}


def command_migrate(args: argparse.Namespace) -> dict[str, Any]:
    tasks = parse_tasks(args.tasks)
    by_id = {task.case_id: task for task in tasks}
    if args.case_id not in by_id:
        raise format_error("unknown case_id", task_id=args.case_id, field="case_id")
    return migrate_snapshot(
        args.source,
        by_id[args.case_id],
        target_format=args.to,
        output=args.output,
        limits=_limits(args),
    )


def command_revise(args: argparse.Namespace) -> dict[str, Any]:
    request = RevisionRequest(
        case_id=args.case_id,
        revision=args.revision,
        lr=args.lr,
        adapter_dim=args.adapter_dim,
        steps=args.steps,
        micro_batch=args.micro_batch,
        seed=args.seed,
        snapshot_path=args.snapshot,
    )
    service = RevisionService(
        tasks_path=args.tasks,
        catalog_path=args.catalog,
        state_dir=args.state_dir,
        limits=_limits(args),
    )
    revision = service.apply([request])
    result: dict[str, Any] = {"revision": revision, "training": None}
    if args.run:
        run_id = args.new_run_id or f"revision-g{revision['config_generation']}"
        training = DiagnosticEngine(
            EngineConfig(
                tasks_path=args.tasks,
                catalog_path=args.catalog,
                state_dir=args.state_dir,
                run_id=run_id,
                snapshot_format=args.snapshot_format,
                limits=_limits(args),
            )
        ).run()
        result["training"] = _public_report(training)
    return result


def command_replay(args: argparse.Namespace) -> dict[str, Any]:
    tasks = parse_tasks(args.tasks)
    by_id = {task.case_id: task for task in tasks}
    if args.case_id not in by_id:
        raise format_error("unknown case_id", task_id=args.case_id, field="case_id")
    catalog = Catalog(args.catalog)
    snapshot_path = catalog.snapshot_path(args.case_id, args.snapshot_format)
    return replay_task(
        artifact_path=args.artifact,
        task=by_id[args.case_id],
        current_snapshot_path=snapshot_path,
        limits=_limits(args),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="peft-diag", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--output", required=True)
    generate.add_argument("--seed", type=int, default=20261127)
    generate.add_argument("--task-count", type=int, default=None)
    generate.set_defaults(handler=command_generate)

    validate = subparsers.add_parser("validate", help="validate task list and snapshot structure")
    validate.add_argument("--tasks", required=True)
    validate.add_argument("--catalog")
    validate.add_argument("--snapshot-format", choices=("v1", "v2"), default="v2")
    validate.add_argument("--all-formats", action="store_true")
    _add_limit_arguments(validate)
    validate.set_defaults(handler=command_validate)

    train = subparsers.add_parser("train", help="run or recover staged PEFT training")
    _add_engine_arguments(train)
    train.add_argument("--no-recover", action="store_true")
    train.add_argument("--no-signal-handlers", action="store_true")
    train.add_argument("--simulate-kill-at", default=None, help="STAGE:CYCLE:TASK_INDEX force-crash hook")
    train.set_defaults(handler=command_train)

    status = subparsers.add_parser("status", help="show checkpoint, cache and revision state")
    status.add_argument("--state-dir", required=True)
    status.add_argument("--run-id", default="primary")
    status.add_argument("--max-cache-entries", type=int, default=None)
    status.set_defaults(handler=command_status)

    dashboard = subparsers.add_parser("dashboard", help="lightweight state-machine dashboard")
    dashboard.add_argument("--state-dir", required=True)
    dashboard.add_argument("--run-id", default="primary")
    dashboard.add_argument("--max-cache-entries", type=int, default=None)
    dashboard.add_argument("--interactive", action="store_true")
    dashboard.set_defaults(handler=command_dashboard)

    migrate = subparsers.add_parser("migrate", help="bidirectionally migrate a v1/v2 snapshot")
    migrate.add_argument("--tasks", required=True)
    migrate.add_argument("--case-id", required=True)
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--to", required=True, choices=("v1", "v2"))
    migrate.add_argument("--output", required=True)
    _add_limit_arguments(migrate)
    migrate.set_defaults(handler=command_migrate)

    revise = subparsers.add_parser("revise", help="commit a targeted revision transaction")
    revise.add_argument("--tasks", required=True)
    revise.add_argument("--catalog", required=True)
    revise.add_argument("--state-dir", required=True)
    revise.add_argument("--case-id", required=True)
    revise.add_argument("--revision", type=int, default=None)
    revise.add_argument("--lr", type=float, default=None)
    revise.add_argument("--adapter-dim", type=int, default=None)
    revise.add_argument("--steps", type=int, default=None)
    revise.add_argument("--micro-batch", type=int, default=None)
    revise.add_argument("--seed", type=int, default=None)
    revise.add_argument("--snapshot", default=None)
    revise.add_argument("--run", action="store_true")
    revise.add_argument("--new-run-id", default=None)
    revise.add_argument("--snapshot-format", choices=("v1", "v2"), default="v2")
    _add_limit_arguments(revise)
    revise.set_defaults(handler=command_revise)

    replay = subparsers.add_parser("replay", help="replay a traced task artifact without cache")
    replay.add_argument("--tasks", required=True)
    replay.add_argument("--catalog", required=True)
    replay.add_argument("--case-id", required=True)
    replay.add_argument("--artifact", required=True)
    replay.add_argument("--snapshot-format", choices=("v1", "v2"), default="v2")
    _add_limit_arguments(replay)
    replay.set_defaults(handler=command_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        _print(result)
        return 0
    except BusinessError as exc:
        _print(exc.to_dict())
        return 2
    except FileNotFoundError as exc:
        _print(
            BusinessError(
                "E_FORMAT",
                f"required local file is missing: {exc}",
                field="path",
            ).to_dict()
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

