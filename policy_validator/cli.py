"""Command-line entry point.

Usage:
    python -m policy_validator run --policies policies.json
        [--tasks tasks.json] --workdir out/ [--resume]
        [--concurrency N] [--task-timeout SEC] [--queue-limit N]
        [--task-memory-limit BYTES] [--batch-memory-budget BYTES]

Exit codes are a bitmask (see policy_validator.errors):
  0 ok | 1 rejections | 2 findings | 4 resource events | 8 lock conflict
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from . import errors
from .checkpoint import CheckpointStore
from .detector import detect_cycles
from .models import TaskSpec
from .parser import parse_policies
from .report import build_report, exit_code_for
from .scheduler import SchedulerConfig, plan_tasks, run_batch
from .storage import LockConflict, locked_atomic_write_json


def _fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_tasks(path: Path) -> list[TaskSpec]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    specs = []
    for entry in doc.get("tasks", []):
        specs.append(TaskSpec(
            task_id=str(entry["task_id"]),
            target=str(entry["target"]),
            rule_ids=tuple(sorted(str(r) for r in entry.get("rule_ids", []))),
            simulate=entry.get("simulate"),
        ))
    return sorted(specs, key=lambda t: t.task_id)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="policy_validator")
    parser.add_argument("--policies", required=True, type=Path)
    parser.add_argument("--tasks", type=Path, default=None)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--task-timeout", type=float, default=5.0)
    parser.add_argument("--queue-limit", type=int, default=64)
    parser.add_argument("--task-memory-limit", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--batch-memory-budget", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--batch-id", default=None)
    args = parser.parse_args(argv)

    args.workdir.mkdir(parents=True, exist_ok=True)
    raw_bytes = args.policies.read_bytes()
    fingerprint = _fingerprint(raw_bytes)
    doc = json.loads(raw_bytes.decode("utf-8"))
    parsed = parse_policies(doc)
    rules_by_id = {r.rule_id: r for r in parsed.rules}

    tasks = _load_tasks(args.tasks) if args.tasks else plan_tasks(parsed.rules)
    config = SchedulerConfig(
        concurrency=args.concurrency,
        task_timeout_sec=args.task_timeout,
        queue_limit=args.queue_limit,
        task_memory_limit_bytes=args.task_memory_limit,
        batch_memory_budget_bytes=args.batch_memory_budget,
    )
    batch_id = args.batch_id or uuid.uuid4().hex[:12]
    store = CheckpointStore(args.workdir, batch_id, fingerprint)

    events: list[dict[str, str]] = []
    resumed_state = None
    if args.resume:
        resumed_state, recover_events = store.recover()
        events.extend(recover_events)

    def checkpoint_save(state: dict) -> None:
        full = store.initial_state()
        full.update(state)
        store.save(full)

    try:
        batch = run_batch(
            tasks, rules_by_id, config,
            resumed_state=resumed_state,
            checkpoint_save=checkpoint_save,
        )
        cycles = detect_cycles(parsed.rules)
        report = build_report(
            batch_id=batch_id,
            policy_fingerprint=fingerprint,
            config_snapshot=asdict(config),
            rejections=parsed.rejections,
            outcomes=batch.outcomes,
            cycles=cycles,
            events=events + batch.events,
            conclusion=batch.conclusion,
            budget_consumed_bytes=batch.budget_consumed_bytes,
        )
        locked_atomic_write_json(
            args.workdir / "report.json", report,
            required_keys=("report_version", "summary", "findings"))
    except LockConflict:
        print(f"error: {errors.E_LOCK_CONFLICT} lock conflict in {args.workdir}",
              file=sys.stderr)
        return errors.EXIT_LOCK_CONFLICT

    return exit_code_for(report)


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
