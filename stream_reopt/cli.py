from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import CommitResult, Engine
from .errors import EngineError
from .sample import load_sample
from .storage import read_status_without_sample


EXIT_CODES = {
    "E_BAD_SAMPLE": 2,
    "E_BAD_TABLE": 2,
    "E_BAD_PLAN": 2,
    "E_BAD_STATS": 2,
    "E_BAD_CONFIG": 2,
    "E_BAD_BATCH": 2,
    "E_SWITCH_BUDGET": 3,
    "E_VERIFY_FAILED": 4,
    "E_NO_STATE": 5,
    "E_STATE_MISMATCH": 5,
}


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2))


def _handle_error(error: EngineError) -> int:
    _emit(error.to_dict())
    return EXIT_CODES.get(error.code, 1)


def _result_payload(result: CommitResult) -> dict[str, Any]:
    payload = result.to_dict()
    if payload["observation"] is not None:
        observation = payload["observation"]
        payload["observation"] = {
            key: observation[key]
            for key in (
                "seq",
                "arrival",
                "committed_count",
                "input_rows",
                "output_rows",
                "observed_selectivity",
                "result_digest",
                "shape_version",
            )
        }
        payload["observation"]["drift_source_count"] = len(observation["drift_sources"])
    return payload


def _run_serial(
    sample_path: str,
    db_path: str,
    batches: int | None,
    crash_arrival: int | None,
) -> tuple[int, dict[str, Any]]:
    sample = load_sample(sample_path)
    with Engine(sample, db_path) as engine:
        results = engine.run(max_batches=batches, crash_arrival=crash_arrival)
        error_code = next(
            (result.error_code for result in results if result.error_code == "E_SWITCH_BUDGET"),
            None,
        )
        payload = {
            "ok": error_code is None,
            "command": "run",
            "processed": [result.batch["arrival"] for result in results if result.batch],
            "results": [_result_payload(result) for result in results],
            "status": engine.status(observation_limit=0),
        }
        if error_code is not None:
            payload["error"] = {
                "code": error_code,
                "message": "re-optimization was triggered but the switch cost budget was insufficient",
            }
        return (EXIT_CODES.get(error_code, 1) if error_code else 0), payload


def _worker(sample_path: str, db_path: str, target: int, queue: Any) -> None:
    try:
        sample = load_sample(sample_path)
        processed: list[int] = []
        errors: list[str] = []
        warnings: list[str] = []
        with Engine(sample, db_path) as engine:
            while True:
                state = engine.storage.state()
                if state.committed_count >= target:
                    break
                result = engine.advance_one()
                if result is None:
                    break
                if result.batch is not None:
                    processed.append(result.batch["arrival"])
                if result.error_code is not None:
                    if result.error_code == "E_SWITCH_BUDGET":
                        errors.append(result.error_code)
                        break
                    warnings.append(result.error_code)
        queue.put(
            {
                "ok": not errors,
                "processed": sorted(processed),
                "errors": errors,
                "warnings": warnings,
            }
        )
    except EngineError as error:
        queue.put({"ok": False, "processed": [], "errors": [error.code], "error": error.to_dict()})
    except BaseException as error:
        queue.put({"ok": False, "processed": [], "errors": ["E_IO"], "error": repr(error)})


def _run_parallel(
    sample_path: str,
    db_path: str,
    workers: int,
    batches: int | None,
) -> tuple[int, dict[str, Any]]:
    sample = load_sample(sample_path)
    with Engine(sample, db_path) as engine:
        initial = engine.storage.state()
        total = len(engine.generator.batch_specs)
    target = total if batches is None else min(total, initial.committed_count + batches)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_worker, args=(sample_path, db_path, target, queue))
        for _ in range(max(1, workers))
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join()
    worker_results = [queue.get() for _ in processes]
    assert all(process.exitcode == 0 for process in processes)
    processed = sorted({arrival for result in worker_results for arrival in result.get("processed", [])})
    errors = sorted({code for result in worker_results for code in result.get("errors", [])})
    with Engine(sample, db_path) as engine:
        status = engine.status(observation_limit=0)
    error_code = "E_SWITCH_BUDGET" if "E_SWITCH_BUDGET" in errors else None
    payload = {
        "ok": not errors,
        "command": "run",
        "workers": workers,
        "processed": processed,
        "worker_results": sorted(worker_results, key=lambda item: item["processed"]),
        "status": status,
    }
    if errors:
        payload["errors"] = errors
    return (EXIT_CODES.get(error_code, 1) if error_code else 0), payload


def cmd_run(args: argparse.Namespace) -> int:
    if args.batches is not None and args.batches < 0:
        raise EngineError("E_BAD_CONFIG", "--batches must be >= 0", {"batches": args.batches})
    if args.workers > 1 and args.crash_before_commit_arrival is not None:
        raise EngineError(
            "E_BAD_CONFIG",
            "crash injection requires --workers 1",
            {"workers": args.workers},
        )
    if args.workers > 1:
        code, payload = _run_parallel(args.sample, args.db, args.workers, args.batches)
    else:
        code, payload = _run_serial(
            args.sample,
            args.db,
            args.batches,
            args.crash_before_commit_arrival,
        )
    _emit(payload)
    return code


def cmd_resume(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise EngineError("E_NO_STATE", "database does not exist", {"path": args.db})
    return cmd_run(args)


def cmd_observe(args: argparse.Namespace) -> int:
    if args.last is not None and args.last < 0:
        raise EngineError("E_BAD_CONFIG", "--last must be >= 0", {"last": args.last})
    if not Path(args.db).exists():
        raise EngineError("E_NO_STATE", "database does not exist", {"path": args.db})
    sample = load_sample(args.sample)
    with Engine(sample, args.db) as engine:
        payload = engine.observe(limit=args.last)
    _emit(payload)
    return 0


def cmd_reopt(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise EngineError("E_NO_STATE", "database does not exist", {"path": args.db})
    sample = load_sample(args.sample)
    with Engine(sample, args.db) as engine:
        payload = engine.force_reopt(force=args.force)
    _emit(payload)
    if payload.get("error_code") == "E_SWITCH_BUDGET":
        return EXIT_CODES["E_SWITCH_BUDGET"]
    return 0 if payload.get("ok", True) else 1


def cmd_status(args: argparse.Namespace) -> int:
    if args.last is not None and args.last < 0:
        raise EngineError("E_BAD_CONFIG", "--last must be >= 0", {"last": args.last})
    if args.sample:
        if not Path(args.db).exists():
            raise EngineError("E_NO_STATE", "database does not exist", {"path": args.db})
        sample = load_sample(args.sample)
        with Engine(sample, args.db) as engine:
            payload = engine.status(observation_limit=args.last)
    else:
        payload = read_status_without_sample(args.db)
    _emit(payload)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        raise EngineError("E_NO_STATE", "database does not exist", {"path": args.db})
    sample = load_sample(args.sample)
    with Engine(sample, args.db) as engine:
        payload = engine.verify()
    _emit(payload)
    return 0 if payload.get("ok") else EXIT_CODES["E_VERIFY_FAILED"]


def _add_common_runtime_args(parser: argparse.ArgumentParser, include_batches: bool = True) -> None:
    parser.add_argument("--sample", required=True, help="local JSON sample path")
    parser.add_argument("--db", required=True, help="SQLite state path")
    if include_batches:
        parser.add_argument("--batches", type=int, default=None, help="maximum number of new batches")
        parser.add_argument("--workers", type=int, default=1, help="number of executor processes")
        parser.add_argument(
            "--crash-before-commit-arrival",
            type=int,
            default=None,
            help="test hook: SIGKILL after transaction writes but before COMMIT",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stream-reopt")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="advance the streaming plan")
    _add_common_runtime_args(run_parser)
    run_parser.set_defaults(handler=cmd_run)

    resume_parser = subparsers.add_parser("resume", help="resume an existing runtime state")
    _add_common_runtime_args(resume_parser)
    resume_parser.set_defaults(handler=cmd_resume)

    observe_parser = subparsers.add_parser("observe", help="read committed observations")
    _add_common_runtime_args(observe_parser, include_batches=False)
    observe_parser.add_argument("--last", type=int, default=None)
    observe_parser.set_defaults(handler=cmd_observe)

    reopt_parser = subparsers.add_parser("reopt", help="evaluate a re-optimization at the current boundary")
    _add_common_runtime_args(reopt_parser, include_batches=False)
    reopt_parser.add_argument("--force", action="store_true")
    reopt_parser.set_defaults(handler=cmd_reopt)

    status_parser = subparsers.add_parser("status", help="emit stable runtime status JSON")
    status_parser.add_argument("--db", required=True)
    status_parser.add_argument("--sample", default=None)
    status_parser.add_argument("--last", type=int, default=None)
    status_parser.set_defaults(handler=cmd_status)

    verify_parser = subparsers.add_parser("verify", help="verify deterministic batches, observations and joins")
    _add_common_runtime_args(verify_parser, include_batches=False)
    verify_parser.set_defaults(handler=cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except EngineError as error:
        return _handle_error(error)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
