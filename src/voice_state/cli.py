"""Command-line entry point and Windows-friendly local executable surface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .app import run_interactive
from .canonical import atomic_write_bytes, canonical_json_bytes
from .config import AnalysisConfig
from .demo import run_demo
from .diagnostics import recent_errors, record_error
from .engine import AnalysisEngine
from .errors import BusinessError, E_CONFIG_INVALID, E_INCONSISTENT
from .selfcheck import run_selfcheck
from .snapshot import STAGE_ORDER, SnapshotStore
from .synth import append_registry_sample, generate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-emotion",
        description="Deterministic local dialog emotion analysis with recoverable stage snapshots.",
    )
    parser.add_argument(
        "--workdir",
        default=str(Path.cwd() / "voice_state_data"),
        help="Workspace used for default dataset and state paths.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("app", help="Start the interactive local application.")

    generate = sub.add_parser("generate", help="Generate deterministic local sample data.")
    generate.add_argument("--dataset", default=None)
    generate.add_argument("--dialogs", type=int, default=5)
    generate.add_argument("--speakers", type=int, default=3)
    generate.add_argument("--seed", type=int, default=20261120)
    generate.add_argument("--force", action="store_true")

    for name, help_text in (
        ("run", "Run all four stages, or resume from committed snapshots."),
        ("resume", "Alias of run; always continues from committed snapshots."),
    ):
        command = sub.add_parser(name, help=help_text)
        _add_run_arguments(command)

    status = sub.add_parser("status", help="Show stage state and recent errors.")
    status.add_argument("--dataset", default=None)
    status.add_argument("--state", default=None)

    report = sub.add_parser("report", help="Show or export the aggregation report.")
    report.add_argument("--dataset", default=None)
    report.add_argument("--state", default=None)
    report.add_argument("--output", default=None)
    report.add_argument("--raw", action="store_true")

    features = sub.add_parser("features", help="Export deterministic framing features for one dialog.")
    features.add_argument("--dataset", default=None)
    features.add_argument("--state", default=None)
    features.add_argument("--dialog-id", required=True)
    features.add_argument("--output", default=None)
    features.add_argument("--frame-ms", type=float, default=20.0)
    features.add_argument("--hop-ms", type=float, default=20.0)
    _add_limits(features)

    check = sub.add_parser("selfcheck", help="Run cross-module consistency checks.")
    check.add_argument("--dataset", default=None)
    check.add_argument("--state", default=None)
    check.add_argument("--full-report", default=None)
    check.add_argument("--recovered-report", default=None)

    demo = sub.add_parser("demo", help="Crash after a stage, resume, and compare reports.")
    demo.add_argument("--stop-after", choices=STAGE_ORDER, default="segmentation")
    demo.add_argument("--dialogs", type=int, default=4)
    demo.add_argument("--speakers", type=int, default=3)
    _add_limits(demo)

    append = sub.add_parser("append-registry", help="Append a deterministic speaker sample.")
    append.add_argument("--registry", default=None)
    append.add_argument("--speaker-id", type=int, required=True)
    append.add_argument("--clip-seed", type=int, required=True)
    append.add_argument("--clip-ms", type=int, default=840)

    return parser


def _add_run_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--dataset", default=None)
    command.add_argument("--state", default=None)
    command.add_argument("--crash-after-stage", choices=STAGE_ORDER, default=None)
    command.add_argument("--crash-during-stage", choices=STAGE_ORDER, default=None)
    command.add_argument("--crash-after-dialogs", type=int, default=0)
    command.add_argument("--frame-ms", type=float, default=20.0)
    command.add_argument("--hop-ms", type=float, default=20.0)
    _add_limits(command)


def _add_limits(command: argparse.ArgumentParser) -> None:
    command.add_argument("--max-dialogs", type=int, default=1000)
    command.add_argument("--max-resident-bytes", type=int, default=64 * 1024 * 1024)
    command.add_argument("--max-segment-bytes", type=int, default=32 * 1024 * 1024)
    command.add_argument("--batch-size", type=int, default=0)


def config_from_args(args: argparse.Namespace) -> AnalysisConfig:
    return AnalysisConfig(
        frame_ms=float(getattr(args, "frame_ms", 20.0)),
        hop_ms=float(getattr(args, "hop_ms", 20.0)),
        max_dialogs_per_stage=int(getattr(args, "max_dialogs", 1000)),
        max_resident_feature_bytes=int(getattr(args, "max_resident_bytes", 64 * 1024 * 1024)),
        max_segment_memory_bytes=int(getattr(args, "max_segment_bytes", 32 * 1024 * 1024)),
        batch_size=int(getattr(args, "batch_size", 0)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workdir = Path(args.workdir)
    dataset_dir = Path(args.dataset) if getattr(args, "dataset", None) else workdir / "dataset"
    state_dir = Path(args.state) if getattr(args, "state", None) else workdir / "state"
    if args.command is None or args.command == "app":
        return run_interactive(workdir, dataset_dir=dataset_dir, state_dir=state_dir)
    try:
        if args.command == "generate":
            result = generate_dataset(
                dataset_dir,
                dialog_count=args.dialogs,
                speaker_count=args.speakers,
                seed=args.seed,
                force=args.force,
            )
            _emit(result, args.json)
            return 0
        if args.command in {"run", "resume"}:
            return _command_run(args, dataset_dir, state_dir)
        if args.command == "status":
            return _command_status(args, state_dir)
        if args.command == "report":
            return _command_report(args, dataset_dir, state_dir)
        if args.command == "features":
            return _command_features(args, dataset_dir, state_dir)
        if args.command == "selfcheck":
            return _command_selfcheck(args, dataset_dir, state_dir)
        if args.command == "demo":
            cfg = config_from_args(args)
            result = run_demo(
                workdir,
                stop_after=args.stop_after,
                config=cfg,
                dialog_count=args.dialogs,
                speaker_count=args.speakers,
            )
            _emit(result, args.json)
            return 0
        if args.command == "append-registry":
            registry = Path(args.registry) if args.registry else dataset_dir / "registrations.jsonl"
            result = append_registry_sample(
                registry,
                speaker_id=args.speaker_id,
                clip_seed=args.clip_seed,
                clip_ms=args.clip_ms,
            )
            _emit(result, args.json)
            return 0
        parser.error("Unknown command")
    except BusinessError as exc:
        try:
            record_error(state_dir, exc)
        except OSError:
            pass
        if args.json:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"[{exc.code}] {exc.message}", file=sys.stderr)
            if exc.details:
                print(f"details: {dict(exc.details)}", file=sys.stderr)
        return 2


def _command_run(args: argparse.Namespace, dataset_dir: Path, state_dir: Path) -> int:
    config = config_from_args(args)
    engine = AnalysisEngine(dataset_dir, state_dir, config)
    crash_after = args.crash_after_stage
    crash_during = args.crash_during_stage
    crash_count = args.crash_after_dialogs
    if crash_during and crash_count <= 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            "crash-after-dialogs must be positive when crash-during-stage is used",
            {"field": "crash_after_dialogs", "actual": crash_count},
        )

    def event_hook(event: str, stage: str, completed: int, total: int) -> None:
        if event == "stage_committed" and crash_after == stage:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(75)
        if (
            event == "dialog_completed"
            and crash_during == stage
            and completed >= crash_count
        ):
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(75)

    result = engine.run(on_event=event_hook)
    atomic_write_bytes(state_dir / "report.json", result.report_bytes)
    _emit(result.summary(), args.json)
    return 0


def _command_status(args: argparse.Namespace, state_dir: Path) -> int:
    store = SnapshotStore(state_dir)
    status = store.status()
    status["recent_errors"] = recent_errors(state_dir, 5)
    _emit(status, args.json)
    return 0


def _command_report(args: argparse.Namespace, dataset_dir: Path, state_dir: Path) -> int:
    store = SnapshotStore(state_dir)
    status = store.status()
    if status["stages"].get("aggregation", {}).get("status") != "committed":
        raise BusinessError(
            E_INCONSISTENT,
            "Aggregation has not been committed",
            {"stage": "aggregation"},
        )
    payload = store.load_stage("aggregation")
    report = payload["report"]
    report_bytes = canonical_json_bytes(report)
    if args.output:
        atomic_write_bytes(Path(args.output), report_bytes)
        if args.raw:
            sys.stdout.write(report_bytes.decode("utf-8"))
        else:
            _emit(
                {
                    "ok": True,
                    "output": str(Path(args.output)),
                    "report_hash": report["report_hash"],
                    "report_bytes": len(report_bytes),
                },
                args.json,
            )
    elif args.raw:
        sys.stdout.write(report_bytes.decode("utf-8"))
    else:
        _emit(report, args.json)
    return 0


def _command_selfcheck(args: argparse.Namespace, dataset_dir: Path, state_dir: Path) -> int:
    store = SnapshotStore(state_dir)
    expected_ids = None
    full_bytes = Path(args.full_report).read_bytes() if args.full_report else None
    recovered_bytes = (
        Path(args.recovered_report).read_bytes() if args.recovered_report else None
    )
    result = run_selfcheck(
        store,
        expected_dialog_ids=expected_ids,
        full_report_bytes=full_bytes,
        recovered_report_bytes=recovered_bytes,
    )
    _emit(result, args.json)
    return 0


def _command_features(args: argparse.Namespace, dataset_dir: Path, state_dir: Path) -> int:
    engine = AnalysisEngine(dataset_dir, state_dir, config_from_args(args))
    engine.run_stage("framing")
    payload = engine.store.load_stage("framing")
    record = next(
        (item for item in payload["dialogs"] if item["dialog_id"] == args.dialog_id),
        None,
    )
    if record is None:
        raise BusinessError(
            E_CONFIG_INVALID,
            "Unknown dialog_id in the framing snapshot",
            {"field": "dialog_id", "dialog_id": args.dialog_id},
        )
    data = b"".join(canonical_json_bytes(row) for row in record["features"])
    if args.output:
        atomic_write_bytes(Path(args.output), data)
        _emit(
            {
                "ok": True,
                "dialog_id": args.dialog_id,
                "num_frames": record["num_frames"],
                "features_checksum": record["features_checksum"],
                "output": str(Path(args.output)),
            },
            args.json,
        )
    else:
        sys.stdout.write(data.decode("utf-8"))
    return 0


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and value.get("ok") is True and "ranking" in value:
        print(
            f"Run complete: {value.get('report_hash')} | computed={value.get('computed_stages')} | "
            f"reused={value.get('reused_stages')}"
        )
        for row in value.get("ranking", []):
            print(
                f"{row['rank']:>2}. {row['dialog_id']} "
                f"valence={row['weighted_valence']:.6f} arousal={row['weighted_arousal']:.6f}"
            )
        return
    if isinstance(value, dict) and "stages" in value:
        print(
            f"Stage: {value.get('current_stage')} | last committed: "
            f"{value.get('last_committed_stage')} | generation: {value.get('generation', 0)}"
        )
        for stage, record in value["stages"].items():
            print(
                f"{stage:<20} {record.get('status','pending'):<10} "
                f"version={record.get('version','-')} dialogs={record.get('dialog_count','-')}"
            )
        errors = value.get("recent_errors", [])
        if errors:
            print("Recent errors:")
            for error in errors:
                print(f"  [{error['code']}] {error.get('message','')}")
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())

