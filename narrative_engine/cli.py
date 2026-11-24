from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .branches import explore_branches, explore_choice_sequences, precompute_sequence_chunks
from .checkpoint import CheckpointStore, verify_snapshot_file
from .encoding import atomic_write_json, short_digest
from .engine import ReplayEngine, StopController, run_choice_sequence
from .errors import EXIT_INTERRUPTED, NarrativeError, SchemaError
from .generator import DEFAULT_SEED, generate_sample
from .reporting import build_run_report, verify_report_correspondence
from .schema import load_choice_file, load_script


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="narrative-engine", description="Deterministic fixed-point narrative engine")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate-sample", help="generate deterministic local sample files")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.set_defaults(handler=cmd_generate_sample)

    validate = subparsers.add_parser("validate", help="validate a JSON script")
    validate.add_argument("--script", required=True)
    validate.add_argument("--states")
    validate.add_argument("--choices")
    validate.set_defaults(handler=cmd_validate)

    run = subparsers.add_parser("run", help="run one deterministic choice sequence with checkpoints")
    run.add_argument("--script", required=True)
    run.add_argument("--states")
    run.add_argument("--choices", required=True)
    run.add_argument("--sequence-id")
    run.add_argument("--state-dir", default="runtime/state")
    run.add_argument("--report", default="runtime/run_report.json")
    run.add_argument("--max-events", type=int, default=100_000)
    run.add_argument("--stop-after-events", type=int)
    run.add_argument("--event-delay-ms", type=int, default=0)
    run.add_argument("--no-resume", action="store_true")
    run.set_defaults(handler=cmd_run)

    explore = subparsers.add_parser("explore", help="explore all reachable branches or precompute sequences")
    explore.add_argument("--script", required=True)
    explore.add_argument("--states")
    explore.add_argument("--choice-sequences")
    explore.add_argument("--report", default="runtime/branch_report.json")
    explore.add_argument("--max-depth", type=int, default=64)
    explore.add_argument("--max-paths", type=int, default=10_000)
    explore.add_argument("--reverse-choice-order", action="store_true")
    explore.add_argument("--workers", type=int, default=1)
    explore.add_argument("--chunk-size", type=int, default=4)
    explore.add_argument("--memory-budget-mb", type=int, default=256)
    explore.add_argument("--no-logs", action="store_true")
    explore.set_defaults(handler=cmd_explore)

    verify = subparsers.add_parser("verify-snapshot", help="verify one snapshot digest and sequence")
    verify.add_argument("--path", required=True)
    verify.set_defaults(handler=cmd_verify_snapshot)
    return parser


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def cmd_generate_sample(args: argparse.Namespace) -> int:
    paths = generate_sample(args.out, seed=args.seed)
    emit_json(
        {
            "ok": True,
            "seed": args.seed,
            "paths": {name: str(path) for name, path in paths.items()},
        }
    )
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    script = load_script(args.script, args.states)
    result: dict[str, Any] = {
        "ok": True,
        "version_id": script.version_id,
        "fixed_point_scale": script.scale,
        "node_count": len(script.nodes),
        "event_count": len(script.event_map),
        "choice_count": len(script.choice_map),
        "state_key_count": len(script.states),
    }
    if args.choices:
        choice_file = load_choice_file(args.choices)
        _validate_choice_route(script, choice_file["choices"])
        result["choice_sequence_id"] = choice_file["id"]
        result["choice_count_in_sequence"] = len(choice_file["choices"])
    emit_json(result)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    script = load_script(args.script, args.states)
    choice_file = load_choice_file(args.choices)
    _validate_choice_route(script, choice_file["choices"])
    sequence_id = args.sequence_id or choice_file["id"]
    run_fingerprint = "run-" + short_digest(
        {"sequence_id": sequence_id, "choices": choice_file["choices"], "version_id": script.version_id},
        16,
    )

    store = CheckpointStore(args.state_dir)
    engine: ReplayEngine | None = None
    recovery_info: dict[str, Any] | None = None
    warnings: list[str] = []

    if not args.no_resume:
        recovery = store.recover(
            expected_version=script.version_id,
            run_fingerprint=run_fingerprint,
        )
        if isinstance(recovery, str):
            archived = store.archive_state(recovery)
            recovery_info = {"action": "archived_and_restarted", "reason": recovery, "archive": str(archived)}
        elif recovery is not None:
            engine = ReplayEngine.restore_from_snapshot(script, recovery.body)
            recovery_info = {
                "action": "resumed",
                "source": str(recovery.source),
                "issues": recovery.issues,
            }
            if recovery.issues:
                warnings.append("recovered from a fallback snapshot after digest verification failure")

    if engine is None:
        engine = ReplayEngine.fresh(script)

    controller = StopController(stop_after_events=args.stop_after_events)
    _install_signal_handlers(controller)

    def on_event(record: dict[str, Any], active_engine: ReplayEngine) -> None:
        body = active_engine.snapshot_body(run_fingerprint, status="running")
        store.save_snapshot(body)
        if args.event_delay_ms > 0:
            time.sleep(args.event_delay_ms / 1000.0)

    try:
        run_choice_sequence(
            script,
            choice_file,
            engine=engine,
            on_event=on_event,
            stop_controller=controller,
            max_events=args.max_events,
        )
    except NarrativeError as exc:
        if exc.code != "E_INTERRUPTED":
            raise
        last_digest = None
        if store.snapshot_path.exists():
            try:
                last_digest = verify_snapshot_file(store.snapshot_path).get("digest")
            except NarrativeError:
                last_digest = None
        marker = {
            "event_seq": engine.event_seq,
            "event_ref": f"event:{engine.event_seq}" if engine.event_seq else None,
            "current_node_id": engine.current_node_id,
            "choice_index": engine.choice_index,
            "version_id": script.version_id,
            "run_fingerprint": run_fingerprint,
            "exit_code": EXIT_INTERRUPTED,
            "received_signal": controller.signum,
            "reason": controller.reason,
            "last_snapshot_digest": last_digest,
        }
        store.write_interrupt(marker)
        payload = exc.to_dict()
        payload["interrupt"] = marker
        emit_json(payload)
        return EXIT_INTERRUPTED

    report = build_run_report(
        script,
        engine,
        sequence_id=sequence_id,
        planned_choice_count=len(choice_file["choices"]),
        recovery=recovery_info,
        warnings=warnings,
    )
    correspondence_errors = verify_report_correspondence(report, script)
    if correspondence_errors:
        raise NarrativeError(
            "causal correspondence verification failed",
            details={"issues": correspondence_errors},
            code="E_CAUSAL",
            exit_code=5,
        )
    if args.report:
        atomic_write_json(Path(args.report), report)
    completed_body = engine.snapshot_body(run_fingerprint, status="completed")
    store.save_snapshot(completed_body)
    store.clear_interrupt()
    emit_json(report)
    return 0


def cmd_explore(args: argparse.Namespace) -> int:
    script = load_script(args.script, args.states)
    if args.choice_sequences:
        with Path(args.choice_sequences).open("r", encoding="utf-8") as handle:
            collection = json.load(handle)
        if args.workers <= 1:
            report = explore_choice_sequences(script, collection, include_logs=not args.no_logs)
        else:
            sequences = collection.get("sequences", collection if isinstance(collection, list) else [collection])
            chunks = [sequences[i : i + max(1, args.chunk_size)] for i in range(0, len(sequences), max(1, args.chunk_size))]
            chunk_reports = precompute_sequence_chunks(
                script,
                chunks,
                max_workers=args.workers,
                memory_budget_bytes=max(1, args.memory_budget_mb) * 1024 * 1024,
                include_logs=not args.no_logs,
            )
            merged: list[dict[str, Any]] = []
            for chunk_report in chunk_reports:
                merged.extend(chunk_report["sequences"])
            report = {
                "ok": True,
                "report_type": "concurrent_choice_sequence_precompute",
                "version_id": script.version_id,
                "worker_count": args.workers,
                "chunk_count": len(chunks),
                "sequence_count": len(merged),
                "sequences": merged,
            }
    else:
        report = explore_branches(
            script,
            max_depth=args.max_depth,
            max_paths=args.max_paths,
            reverse_choice_order=args.reverse_choice_order,
            include_logs=not args.no_logs,
        )
    if args.report:
        atomic_write_json(Path(args.report), report)
    summary = {
        key: value
        for key, value in report.items()
        if key
        not in {
            "paths",
            "sequences",
            "deadlocks",
            "deadlock_nodes",
            "node_state_summaries",
        }
    }
    emit_json(summary)
    return 0


def cmd_verify_snapshot(args: argparse.Namespace) -> int:
    body = verify_snapshot_file(args.path)
    emit_json(
        {
            "ok": True,
            "path": args.path,
            "digest": body["digest"],
            "version_id": body.get("version_id"),
            "event_seq": body.get("event_seq"),
            "status": body.get("status"),
        }
    )
    return 0


def _validate_choice_route(script: Any, choices: list[str]) -> None:
    current = script.start_node
    for index, choice_id in enumerate(choices):
        if choice_id not in script.choice_map:
            raise SchemaError(
                f"choice sequence item {index} references unknown choice: {choice_id}",
                details={"index": index, "choice_id": choice_id},
            )
        choice = script.choice_map[choice_id]
        if choice.node_id != current:
            raise SchemaError(
                f"choice {choice_id!r} belongs to node {choice.node_id!r}, but route is at {current!r}",
                details={"index": index, "choice_id": choice_id, "expected_node": current, "choice_node": choice.node_id},
            )
        current = choice.target


def _install_signal_handlers(controller: StopController) -> None:
    def handler(signum: int, _frame: Any) -> None:
        controller.request(signum=signum, reason="termination_signal")

    for signame in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Non-main threads or platforms that do not support a signal.
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except NarrativeError as exc:
        emit_json(exc.to_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        emit_json({"ok": False, "error": {"code": "E_INTERRUPTED", "message": "keyboard interrupt"}})
        return EXIT_INTERRUPTED
    except Exception as exc:  # defensive JSON boundary for integrators
        emit_json(
            {
                "ok": False,
                "error": {
                    "code": "E_ENGINE",
                    "message": str(exc) or exc.__class__.__name__,
                    "type": exc.__class__.__name__,
                },
            }
        )
        return 5


if __name__ == "__main__":
    sys.exit(main())
