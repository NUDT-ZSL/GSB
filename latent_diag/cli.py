from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import EngineError
from .executor import run_engine
from .generator import FIXED_GENERATOR_SEED, generate_samples
from .pipeline import condition_embedding, initial_noise
from .selfcheck import run_selfcheck, write_selfcheck_report
from .snapshots import SnapshotStore
from .task import load_manifest, validate_manifest


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latent-diag",
        description="Deterministic blockwise latent denoising diagnostics",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--out", required=True)
    generate.add_argument("--seed", type=int, default=FIXED_GENERATOR_SEED)

    validate = sub.add_parser("validate", help="validate manifest inputs")
    validate.add_argument("--tasks", required=True)
    validate.add_argument("--config", required=True)

    run = sub.add_parser("run", help="run blockwise denoising and write the ledger")
    run.add_argument("--tasks", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--db", required=True)
    run.add_argument("--state", required=True)
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--batch-size", type=int, default=None)
    run.add_argument("--recompute-committed", action="store_true")
    run.add_argument(
        "--crash-after-checkpoints",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    run.add_argument("--json-out", default=None)

    selfcheck = sub.add_parser("selfcheck", help="run cross-module consistency checks")
    selfcheck.add_argument("--config", required=True)
    selfcheck.add_argument("--db", required=True)
    selfcheck.add_argument("--state", required=True)
    selfcheck.add_argument("--json-out", default=None)

    verify = sub.add_parser("snapshot-verify", help="verify current snapshot bundle")
    verify.add_argument("--state", required=True)

    hold = sub.add_parser("snapshot-hold", help=argparse.SUPPRESS)
    hold.add_argument("--state", required=True)
    hold.add_argument("--seconds", type=float, default=5.0)
    return parser


def _write_json(path: str, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            metadata = generate_samples(args.out, seed=args.seed)
            _print_json(metadata)
            return 0

        if args.command == "validate":
            config = load_config(args.config)
            tasks = load_manifest(args.tasks)
            validate_manifest(tasks, config)
            for task in tasks:
                initial_noise(task)
                condition_embedding(task.prompt, config)
            _print_json(
                {
                    "ok": True,
                    "task_count": len(tasks),
                    "task_ids": [task.task_id for task in tasks],
                    "dtype_profiles": sorted({task.dtype_profile for task in tasks}),
                    "total_denoise_iterations": sum(
                        task.denoise_iterations for task in tasks
                    ),
                }
            )
            return 0

        if args.command == "run":
            summary = run_engine(
                tasks_path=args.tasks,
                config_path=args.config,
                db_path=args.db,
                state_dir=args.state,
                worker_count=args.workers,
                tasks_per_batch=args.batch_size,
                recompute_committed=args.recompute_committed,
                crash_after_checkpoints=args.crash_after_checkpoints,
            )
            if args.json_out:
                _write_json(args.json_out, summary)
            _print_json(summary)
            return 0 if summary.get("status") == "ok" else 1

        if args.command == "selfcheck":
            config = load_config(args.config)
            report = run_selfcheck(
                db_path=args.db,
                state_dir=args.state,
                config=config,
            )
            if args.json_out:
                write_selfcheck_report(report, args.json_out)
            _print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "snapshot-verify":
            report = SnapshotStore(args.state).verify()
            _print_json(report)
            return 0 if report.get("ok") else 1

        if args.command == "snapshot-hold":
            store = SnapshotStore(args.state)
            handle = store.hold_commit_lock()
            try:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "event": "lock_held",
                            "state": str(Path(args.state).resolve()),
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                time.sleep(max(0.0, args.seconds))
            finally:
                handle.close()
            return 0
    except EngineError as exc:
        _print_json(exc.to_dict())
        return 2
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())

