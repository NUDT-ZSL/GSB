from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_json, sha256_value
from .engine import build_replay
from .errors import AnimError, BudgetError, DigestError, ReplayError, SchemaError, VersionError
from .incremental import incremental_replay
from .persistence import StopToken, output_digest, run_persistent_replay
from .reconnect import reconnect_catchup
from .samplegen import generate_samples
from .schema import apply_edits, load_edits, load_input, validate_input, version_id
from .snapshots import merge_snapshots, write_merge_outputs


EXIT_CODES = {
    "E_SCHEMA": 2,
    "E_DIGEST": 3,
    "E_REPLAY": 4,
    "E_VERSION": 5,
    "E_BUDGET": 6,
}


def _emit(value: Any) -> None:
    print(canonical_json(value))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _result_output(result: Any) -> dict[str, Any]:
    return {
        "format": "anim-replay-result-v1",
        "version_id": result.version_id,
        "chunk_count": len(result.chunks),
        "frame_count": len(result.frames),
        "frames": [frame.to_dict(include_dependencies=True) for frame in result.frames],
        "events": [event.to_dict() for event in result.events],
        "content_digest": output_digest(result),
    }


def _resolve_loaded(args: argparse.Namespace):
    loaded = load_input(args.input)
    edit_records = []
    if getattr(args, "edits", None):
        loaded, edit_records = apply_edits(loaded, load_edits(args.edits))
    return loaded, edit_records


def _install_signal_handlers(token: StopToken) -> None:
    def handler(signum: int, _frame: Any) -> None:
        code = 130 if signum == getattr(signal, "SIGINT", 2) else 143
        token.request(code, signal_name=str(signum), reason="signal")

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handler)
        except (OSError, ValueError):
            pass


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_samples(args.out, seed=args.seed)
    _emit(
        {
            "ok": True,
            "out": str(Path(args.out)),
            "seed": args.seed,
            "file_count": len(manifest["files"]),
            "version_base": manifest["summary"]["version_base"],
            "version_edited": manifest["summary"]["version_edited"],
        }
    )
    return 0


def command_validate(args: argparse.Namespace) -> int:
    loaded, edit_records = _resolve_loaded(args)
    spec = loaded.spec
    _emit(
        {
            "ok": True,
            "version_id": version_id(spec),
            "bone_count": len(spec.skeleton),
            "clip_count": len(spec.clips),
            "transition_count": len(spec.transitions),
            "parameter_count": len(spec.parameters),
            "frame_count": len(spec.timeline.times),
            "chunk_count": spec.timeline.duration_ms // spec.timeline.chunk_ms + 1,
            "applied_edit_count": len(edit_records),
        }
    )
    return 0


def command_replay(args: argparse.Namespace) -> int:
    loaded, _edit_records = _resolve_loaded(args)
    token = StopToken()
    _install_signal_handlers(token)
    persistence = run_persistent_replay(
        loaded.spec,
        args.state_dir,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget,
        stop_token=token,
        stop_after_chunks=args.stop_after_chunks,
        resume=not args.no_resume,
    )
    if not persistence.interrupted and args.out:
        output = Path(args.out)
        _atomic_json(output / "replay_result.json", _result_output(persistence.result))
        _atomic_json(output / "replay_report.json", persistence.report)
    _emit(persistence.report)
    return persistence.exit_code


def command_incremental(args: argparse.Namespace) -> int:
    base_loaded = load_input(args.input)
    edits = load_edits(args.edits)
    edited_loaded, _records = apply_edits(base_loaded, edits)
    reference = build_replay(
        edited_loaded.spec,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget,
    )
    result, report = incremental_replay(
        base_loaded,
        edits,
        reference=reference,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget,
        tolerance=args.tolerance,
    )
    report_dict = report.to_dict(
        version_before=version_id(base_loaded.spec),
        version_after=result.version_id,
    )
    report_dict["ok"] = (
        report.events_equal
        and report.first_over_tolerance is None
        and report.preserved_reused_payloads
    )
    if args.out:
        output = Path(args.out)
        _atomic_json(output / "incremental_result.json", _result_output(result))
        _atomic_json(output / "incremental_report.json", report_dict)
    _emit(report_dict)
    return 0 if report_dict["ok"] else 4


def command_merge(args: argparse.Namespace) -> int:
    if not args.v1 and not args.v2:
        raise SchemaError("at least one snapshot source is required")
    result = merge_snapshots(v1_path=args.v1, v2_dir=args.v2, bucket_ms=args.bucket_ms)
    if args.out:
        write_merge_outputs(result, args.out)
    _emit(
        {
            "ok": result["ok"],
            "error": result.get("error"),
            "fallback": result.get("fallback"),
            "record_count": result["record_count"],
            "conflict_count": len(result["conflicts"]),
            "conflicts": result["conflicts"],
            "snapshots": result["snapshots"],
        }
    )
    return 0 if result["ok"] else 3


def command_reconnect(args: argparse.Namespace) -> int:
    loaded, _edit_records = _resolve_loaded(args)
    authoritative = build_replay(
        loaded.spec,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget,
    )
    client_cache = json.loads(Path(args.client_cache).read_text(encoding="utf-8"))
    try:
        report = reconnect_catchup(
            authoritative,
            client_cache,
            tolerance=args.tolerance,
        )
    except ReplayError as exc:
        payload = exc.as_payload()
        if args.out:
            _atomic_json(Path(args.out) / "reconnect_report.json", payload)
        _emit(payload)
        return 4
    if args.out:
        _atomic_json(Path(args.out) / "reconnect_report.json", report)
    _emit(
        {
            "ok": True,
            "missing_pose_count": report["missing_pose_count"],
            "event_replay_count": report["event_replay_count"],
            "operation_count": report["operation_count"],
            "order_contract": report["order_contract"],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anim-replay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--out", required=True)
    generate.add_argument("--seed", type=int, default=20261125)
    generate.set_defaults(handler=command_generate)

    validate = subparsers.add_parser("validate", help="validate skeleton/tracks/rules/edits")
    validate.add_argument("--input", required=True)
    validate.add_argument("--edits")
    validate.set_defaults(handler=command_validate)

    replay = subparsers.add_parser("replay", help="persistent chunked replay and resume")
    replay.add_argument("--input", required=True)
    replay.add_argument("--edits")
    replay.add_argument("--state-dir", required=True)
    replay.add_argument("--out")
    replay.add_argument("--workers", type=int, default=2)
    replay.add_argument("--memory-budget", type=int, default=4 * 1024 * 1024)
    replay.add_argument("--stop-after-chunks", type=int)
    replay.add_argument("--no-resume", action="store_true")
    replay.set_defaults(handler=command_replay)

    incremental = subparsers.add_parser("incremental", help="invalidate and recompute dirty chunks")
    incremental.add_argument("--input", required=True)
    incremental.add_argument("--edits", required=True)
    incremental.add_argument("--out")
    incremental.add_argument("--workers", type=int, default=2)
    incremental.add_argument("--memory-budget", type=int, default=4 * 1024 * 1024)
    incremental.add_argument("--tolerance", type=float, default=1e-6)
    incremental.set_defaults(handler=command_incremental)

    merge = subparsers.add_parser("merge-snapshots", help="merge v1 and v2 snapshots")
    merge.add_argument("--v1")
    merge.add_argument("--v2")
    merge.add_argument("--out")
    merge.add_argument("--bucket-ms", type=int, default=16)
    merge.set_defaults(handler=command_merge)

    reconnect = subparsers.add_parser("reconnect", help="fill offline poses then replay events")
    reconnect.add_argument("--input", required=True)
    reconnect.add_argument("--edits")
    reconnect.add_argument("--client-cache", required=True)
    reconnect.add_argument("--out")
    reconnect.add_argument("--workers", type=int, default=2)
    reconnect.add_argument("--memory-budget", type=int, default=4 * 1024 * 1024)
    reconnect.add_argument("--tolerance", type=float, default=1e-6)
    reconnect.set_defaults(handler=command_reconnect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except AnimError as exc:
        _emit(exc.as_payload())
        return EXIT_CODES.get(exc.code, 1)
    except (OSError, json.JSONDecodeError) as exc:
        payload = {
            "ok": False,
            "error": {"code": "E_SCHEMA", "message": str(exc)},
        }
        _emit(payload)
        return 2


if __name__ == "__main__":
    sys.exit(main())

