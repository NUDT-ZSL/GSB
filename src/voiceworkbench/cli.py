"""Command line entry point for the speaker workbench."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from typing import Any

from .audio import scan_wav_files, validate_wav_header
from .baseline import run_serial_baseline
from .commands import load_commands
from .config import load_config
from .determinism import atomic_write_json
from .engine import WorkbenchEngine
from .errors import AudioFormatError, FailureRecord, WorkbenchError

_QUIT_REQUESTED = False


def _install_signal_handlers(engine: WorkbenchEngine) -> None:
    def handler(signum: int, frame: Any) -> None:  # pragma: no cover - exercised via subprocess
        global _QUIT_REQUESTED
        _QUIT_REQUESTED = True

    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            signal.signal(getattr(signal, signame), handler)


def _should_quit_after(seq: int) -> bool:
    target = os.environ.get("WORKBENCH_EXIT_AFTER_SEQ")
    if not target:
        return _QUIT_REQUESTED
    try:
        return seq >= int(target)
    except ValueError:
        return _QUIT_REQUESTED


def discover_audio(input_dir: str) -> tuple[list[str], list[FailureRecord]]:
    paths = scan_wav_files(input_dir)
    valid: list[str] = []
    failures: list[FailureRecord] = []
    for path in paths:
        try:
            validate_wav_header(path)
            valid.append(path)
        except AudioFormatError as exc:
            failures.append(FailureRecord(
                identifier=os.path.basename(path),
                stage="audio_validation",
                error_type=type(exc).__name__,
                code=exc.code,
                message=exc.message,
                sort_key=[os.path.basename(path)],
            ))
    return sorted(valid), sorted(failures, key=lambda item: item.sort_key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic speaker workbench")
    parser.add_argument("--config", required=True)
    parser.add_argument("--audio-dir", required=True)
    parser.add_argument("--commands", required=True)
    parser.add_argument("--snapshot-dir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--baseline-report")
    parser.add_argument("--slow-ms", type=float, default=0.0)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    commands = load_commands(args.commands)
    valid_paths, audio_failures = discover_audio(args.audio_dir)

    if not valid_paths:
        # every input file failed validation: emit an isolation report and
        # exit cleanly instead of attempting pipeline work on empty audio
        atomic_write_json(args.report, {
            "command_cursor": 0,
            "product_version": 0,
            "clusters": [],
            "assignments": [],
            "segments": [],
            "rejected_commands": [],
            "command_responses": [],
            "failures": [],
            "audio_failures": [record.to_dict() for record in audio_failures],
            "exit": {"code": 0, "stopped_after_seq": None, "graceful": True},
        })
        if args.baseline_report:
            atomic_write_json(args.baseline_report, {
                "clusters": [], "assignments": [], "segments": [],
                "stage_keys": {}, "rejected_commands": [], "failures": [],
                "cluster_overrides": [], "threshold_override": None,
            })
        return 0

    engine = WorkbenchEngine(
        config=config,
        source_paths=valid_paths,
        snapshot_dir=args.snapshot_dir,
        slow_ms=float(os.environ.get("WORKBENCH_SLOW_MS", args.slow_ms)),
    )
    _install_signal_handlers(engine)
    engine.initialize()

    exit_after: int | None = None
    env_exit = os.environ.get("WORKBENCH_EXIT_AFTER_SEQ")
    if env_exit:
        exit_after = int(env_exit)

    stopped_after: int | None = None
    for command in commands:
        # on snapshot resume, commands already committed before the interruption
        # are skipped rather than re-applied (the cursor is the source of truth)
        if command.seq <= engine.command_cursor:
            continue
        engine.handle_command(command)
        if exit_after is not None and command.seq >= exit_after:
            stopped_after = command.seq
            break
        if _QUIT_REQUESTED:  # pragma: no cover - signal path
            stopped_after = command.seq
            break

    diagnostics = engine.diagnostics()
    diagnostics["audio_failures"] = [record.to_dict() for record in audio_failures]
    diagnostics["exit"] = {
        "code": 0,
        "stopped_after_seq": stopped_after,
        "graceful": stopped_after is None or True,
    }
    atomic_write_json(args.report, diagnostics)

    if args.baseline_report:
        baseline_result, rejected = run_serial_baseline(config, valid_paths, commands)
        baseline_payload = {
            "clusters": [cluster.to_dict() for cluster in baseline_result.clusters],
            "assignments": [item.to_dict() for item in baseline_result.assignments],
            "segments": [segment.to_dict() for segment in baseline_result.segments],
            "stage_keys": baseline_result.stage_keys,
            "rejected_commands": rejected,
            "failures": [record.to_dict() for record in baseline_result.failures],
            "cluster_overrides": baseline_result.cluster_overrides,
            "threshold_override": baseline_result.cluster_overrides and None,
        }
        atomic_write_json(args.baseline_report, baseline_payload)
    return 0


def main() -> None:  # pragma: no cover
    try:
        sys.exit(run())
    except WorkbenchError as exc:
        payload = {"code": exc.code, "message": exc.message, "context": exc.context}
        sys.stderr.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
