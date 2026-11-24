from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .errors import DiagnosticError, EXIT_SCHEMA
from .hashing import version_identity
from .injection import prepare_injected_sample
from .persistence import ChunkStore
from .sample_generator import SEED, generate_sample_tree
from .snapshots import DEFAULT_MEMORY_BUDGET_BYTES
from .validation import load_json_file, parse_events, parse_manifest


def _print_error(error: DiagnosticError) -> None:
    payload = {"status": error.code, "error_code": error.code, "message": error.message, "details": error.details}
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)


def _summary(report: Dict[str, Any]) -> str:
    stats = report.get("statistics", {})
    comparison = report.get("comparison", {})
    first = comparison.get("first_mismatch")
    recovery = report.get("recovery", {})
    lines = [
        f"status={report.get('status')} exit_code={report.get('exit_code')}",
        f"version_id={report.get('version_identity', {}).get('version_id')}",
        "decisions="
        + json.dumps(stats.get("decision_counts", {}), ensure_ascii=False, sort_keys=True),
        f"merge_points={stats.get('merge_point_count')} conflicts={stats.get('conflict_count')} "
        f"digest_failures={stats.get('digest_failure_count')} fallbacks={stats.get('shard_fallback_count')}",
    ]
    if first and report.get("status") == "E_REPLAY":
        lines.append(
            "first_mismatch="
            + json.dumps(
                {
                    key: first.get(key)
                    for key in ("logic_ms", "canonical_screen_id", "field", "expected_replay_value", "actual_merged_value")
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if recovery:
        lines.append(
            "recovery="
            + json.dumps(
                {
                    key: recovery.get(key)
                    for key in ("chunk_count", "last_chunk_index", "last_boundary_logic_ms", "invalid_chunk", "interrupted")
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return "\n".join(lines)


def _command_generate(args: argparse.Namespace) -> int:
    path = generate_sample_tree(args.out, seed=args.seed)
    print(f"generated deterministic samples at {path}")
    return 0


def _resolve_analyze_sample(args: argparse.Namespace) -> tuple[Path, Optional[Dict[str, Any]]]:
    sample_dir = Path(args.samples)
    if not args.inject:
        return sample_dir, None

    destination = Path(args.inject_workdir) / args.inject
    injected_dir, case = prepare_injected_sample(
        sample_dir,
        args.inject,
        destination=destination,
        injection_manifest_path=args.injection_manifest,
    )
    print(f"injected case={args.inject} materialized_at={injected_dir}", file=sys.stderr)
    return injected_dir, case


def _command_analyze(args: argparse.Namespace) -> int:
    sample_dir, injection_case = _resolve_analyze_sample(args)
    from .pipeline import run_diagnostic

    exit_code, report = run_diagnostic(
        sample_dir,
        report_path=args.report,
        state_dir=args.state_dir,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget_bytes,
        resume=args.resume,
        interrupt_after_chunks=args.interrupt_after_chunks,
        chunk_delay_ms=args.chunk_delay_ms,
    )
    if injection_case is not None:
        report["injection"] = {key: value for key, value in injection_case.items()}
        if args.report:
            from .persistence import atomic_write_json

            atomic_write_json(Path(args.report), report)
    print(_summary(report))
    if args.report:
        print(f"report={Path(args.report)}")
    return exit_code


def _command_recover(args: argparse.Namespace) -> int:
    sample_dir = Path(args.samples)
    manifest_raw = load_json_file(sample_dir / "sessions.json")
    events_raw = load_json_file(sample_dir / "frame_events.json")
    manifest = parse_manifest(manifest_raw)
    events = parse_events(events_raw, manifest)
    raw_event_list = events_raw.get("events") if isinstance(events_raw, dict) else events_raw
    version_id, _, _ = version_identity(manifest.raw, raw_event_list)
    store = ChunkStore(args.state_dir, version_id)
    recovered = store.recover_latest()
    payload = {
        "status": "RECOVERED",
        "version_id": version_id,
        "state_dir": str(store.state_dir),
        "chunk_count": recovered.chunk_count,
        "last_chunk_index": recovered.last_chunk_index,
        "last_boundary_logic_ms": recovered.last_boundary_logic_ms,
        "invalid_chunk": recovered.invalid_chunk,
        "chunk_boundaries": [chunk.get("boundary_logic_ms") for chunk in recovered.chunks],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _command_gui(args: argparse.Namespace) -> int:
    from .gui import launch_gui

    return launch_gui(samples_dir=args.samples)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen-diag", description="Offline multi-screen casting diagnostic")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=SEED)
    generate.set_defaults(handler=_command_generate)

    analyze = subparsers.add_parser("analyze", help="validate, adjudicate, merge and compare snapshots")
    analyze.add_argument("--samples", default="samples")
    analyze.add_argument("--report", default=None)
    analyze.add_argument("--state-dir", default=None)
    analyze.add_argument("--workers", type=int, default=4)
    analyze.add_argument("--memory-budget-bytes", type=int, default=DEFAULT_MEMORY_BUDGET_BYTES)
    analyze.add_argument("--resume", action="store_true")
    analyze.add_argument("--interrupt-after-chunks", type=int, default=None)
    analyze.add_argument("--chunk-delay-ms", type=int, default=0, help=argparse.SUPPRESS)
    analyze.add_argument("--inject", default=None)
    analyze.add_argument("--inject-workdir", default="out/injected")
    analyze.add_argument("--injection-manifest", default=None)
    analyze.set_defaults(handler=_command_analyze)

    recover = subparsers.add_parser("recover", help="recover the latest digest-valid chunk prefix")
    recover.add_argument("--samples", default="samples")
    recover.add_argument("--state-dir", required=True)
    recover.set_defaults(handler=_command_recover)

    gui = subparsers.add_parser("gui", help="launch the Tkinter diagnostic view")
    gui.add_argument("--samples", default="samples")
    gui.set_defaults(handler=_command_gui)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except DiagnosticError as error:
        _print_error(error)
        return error.exit_code
    except FileNotFoundError as error:
        schema_error = type("WrappedSchemaError", (DiagnosticError,), {"code": "E_SCHEMA", "exit_code": EXIT_SCHEMA})(
            f"input file not found: {error.filename or error}"
        )
        _print_error(schema_error)
        return EXIT_SCHEMA


if __name__ == "__main__":
    raise SystemExit(main())
