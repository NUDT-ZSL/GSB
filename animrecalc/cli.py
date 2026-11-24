from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .edits import dry_run_edits, load_edit_script, parse_edit_script
from .engine import AnimEngine
from .errors import EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, AnimRecalcError
from .pipeline import load_project_files, project_version
from .sample_gen import generate_sample_tree
from .storage import ChunkStore


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        payload = {
            "ok": False,
            "error": {"code": "E_ARGS", "message": message},
        }
        print_json(payload)
        raise SystemExit(EXIT_ERROR)


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False, default=str))


def add_input_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-dir", default=None, help="directory containing generated sample JSON files")
    parser.add_argument("--skeleton", default=None)
    parser.add_argument("--clips", default=None)
    parser.add_argument("--state-machine", default=None)
    parser.add_argument("--spec", "--chunk-spec", dest="spec", default=None)
    parser.add_argument("--edits", default=None)


def resolve_input_paths(args: argparse.Namespace) -> dict[str, Path | None]:
    sample_dir = Path(args.sample_dir) if args.sample_dir else None

    def path_for(value: str | None, sample_name: str) -> Path | None:
        if value:
            return Path(value)
        if sample_dir is not None:
            candidate = sample_dir / sample_name
            return candidate if candidate.exists() else None
        return None

    return {
        "skeleton": path_for(args.skeleton, "skeleton.json"),
        "clips": path_for(args.clips, "clips.json"),
        "state_machine": path_for(args.state_machine, "state_machine.json"),
        "spec": path_for(args.spec, "chunk_spec.json"),
        "edits": path_for(getattr(args, "edits", None), "edit_script.json"),
    }


def load_project_from_args(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Path | None]]:
    paths = resolve_input_paths(args)
    missing = [name for name in ("skeleton", "clips", "state_machine") if paths[name] is None]
    if missing:
        raise AnimRecalcError("E_ARGS", f"missing input arguments/files: {', '.join(missing)}")
    project = load_project_files(paths["skeleton"], paths["clips"], paths["state_machine"], paths["spec"])  # type: ignore[arg-type]
    return project, paths


def install_signal_handlers(stop_event: threading.Event) -> list[int]:
    handled: list[int] = []

    def handler(signum: int, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, handler)
            handled.append(signum)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handler)  # type: ignore[attr-defined]
            handled.append(signal.SIGBREAK)  # type: ignore[attr-defined]
        except (ValueError, OSError):
            pass
    return handled


def command_generate(args: argparse.Namespace) -> int:
    paths = generate_sample_tree(args.out, seed=args.seed)
    print_json({"ok": True, "generated": {key: str(path) for key, path in paths.items()}})
    return EXIT_OK


def command_validate(args: argparse.Namespace) -> int:
    project, paths = load_project_from_args(args)
    edit_count = 0
    if paths["edits"] is not None:
        edits = load_edit_script(paths["edits"])
        dry_run_edits(project, edits)
        edit_count = len(edits)
    print_json(
        {
            "ok": True,
            "code": "OK",
            "version_id": project_version(project),
            "bones": len(project["skeleton"]["bones"]),
            "clips": len(project["clips"]),
            "transitions": len(project["state_machine"]["transitions"]),
            "edits": edit_count,
        }
    )
    return EXIT_OK


def make_engine(args: argparse.Namespace, project: dict[str, Any], stop_event: threading.Event | None = None) -> AnimEngine:
    return AnimEngine(
        project,
        state_dir=args.state_dir,
        max_workers=args.workers,
        memory_budget_bytes=args.memory_budget,
        stop_event=stop_event,
        interrupt_after_chunks=args.interrupt_after,
        chunk_delay_ms=getattr(args, "chunk_delay_ms", 0) or 0,
    )


def command_baseline(args: argparse.Namespace) -> int:
    project, _ = load_project_from_args(args)
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    engine = make_engine(args, project, stop_event)
    report = engine.run_or_resume_full()
    print_json(report)
    if report["status"] == "interrupted":
        return EXIT_INTERRUPTED
    return EXIT_OK if report.get("ok") else EXIT_ERROR


def command_incremental(args: argparse.Namespace) -> int:
    project, paths = load_project_from_args(args)
    if paths["edits"] is None:
        raise AnimRecalcError("E_ARGS", "incremental command requires --edits or edit_script.json in sample dir")
    edits = load_edit_script(paths["edits"])
    # Dry-run before creating/modifying the state directory.
    dry_run_edits(project, edits)
    stop_event = threading.Event()
    install_signal_handlers(stop_event)
    engine = make_engine(args, project, stop_event)
    report = engine.run_edit_script(edits, verify=not args.no_verify)
    print_json(report)
    if report.get("status") == "interrupted":
        return EXIT_INTERRUPTED
    return EXIT_OK if report.get("ok") else EXIT_ERROR


def command_verify_chunk(args: argparse.Namespace) -> int:
    store = ChunkStore(args.state_dir)
    expected_version = args.version
    if args.sample_dir or args.skeleton:
        project, _ = load_project_from_args(args)
        expected_version = expected_version or project_version(project)
    ok, reason, record = store.verify_chunk(args.chunk_index, expected_version=expected_version)
    if not ok:
        error = AnimRecalcError("E_DIGEST", reason, {"chunk_index": args.chunk_index})
        print_json(error.to_json())
        return EXIT_ERROR
    print_json(
        {
            "ok": True,
            "chunk_index": args.chunk_index,
            "version_id": record.get("version_id") if record else None,
            "frame_count": record.get("frame_count") if record else None,
            "digest": record.get("digest") if record else None,
        }
    )
    return EXIT_OK


def command_fault_chunk(args: argparse.Namespace) -> int:
    store = ChunkStore(args.state_dir)
    path = store.chunk_path(args.chunk_index)
    if not path.exists():
        raise AnimRecalcError("E_ARGS", "chunk does not exist", {"path": str(path)})
    if args.mode == "delete":
        path.unlink()
    elif args.mode == "truncate":
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: max(1, len(text) // 2)], encoding="utf-8")
    elif args.mode == "frame-count":
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["frame_count"] = int(payload.get("frame_count", 1)) + 1
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pose = payload.get("pose")
        if isinstance(pose, list) and pose and isinstance(pose[0], list) and pose[0]:
            pose[0][0][0] = float(pose[0][0][0]) + args.delta
        path.write_text(json.dumps(payload), encoding="utf-8")
    print_json({"ok": True, "fault": args.mode, "chunk_index": args.chunk_index})
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="animrecalc", description="Deterministic skeletal animation incremental recomputation")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-samples")
    gen.add_argument("--out", default="samples")
    gen.add_argument("--seed", type=int, default=20261125)
    gen.set_defaults(func=command_generate)

    validate = sub.add_parser("validate")
    add_input_args(validate)
    validate.set_defaults(func=command_validate)

    baseline = sub.add_parser("baseline", help="evaluate or recover all chunks for the base input")
    add_input_args(baseline)
    baseline.add_argument("--state-dir", default="artifacts/state")
    baseline.add_argument("--workers", type=int, default=None)
    baseline.add_argument("--memory-budget", type=int, default=None)
    baseline.add_argument("--interrupt-after", type=int, default=None)
    baseline.add_argument("--chunk-delay-ms", type=int, default=0, help=argparse.SUPPRESS)
    baseline.set_defaults(func=command_baseline)

    incremental = sub.add_parser("incremental", help="apply edit_script.json with dependency closure")
    add_input_args(incremental)
    incremental.add_argument("--state-dir", default="artifacts/state")
    incremental.add_argument("--workers", type=int, default=None)
    incremental.add_argument("--memory-budget", type=int, default=None)
    incremental.add_argument("--interrupt-after", type=int, default=None)
    incremental.add_argument("--chunk-delay-ms", type=int, default=0, help=argparse.SUPPRESS)
    incremental.add_argument("--no-verify", action="store_true")
    incremental.set_defaults(func=command_incremental)

    recover = sub.add_parser("recover", help="recover matching valid chunks and recompute missing ones")
    add_input_args(recover)
    recover.add_argument("--state-dir", default="artifacts/state")
    recover.add_argument("--workers", type=int, default=None)
    recover.add_argument("--memory-budget", type=int, default=None)
    recover.add_argument("--interrupt-after", type=int, default=None)
    recover.add_argument("--chunk-delay-ms", type=int, default=0, help=argparse.SUPPRESS)
    recover.set_defaults(func=command_baseline)

    verify_chunk = sub.add_parser("verify-chunk")
    add_input_args(verify_chunk)
    verify_chunk.add_argument("--state-dir", default="artifacts/state")
    verify_chunk.add_argument("--chunk-index", type=int, required=True)
    verify_chunk.add_argument("--version", default=None)
    verify_chunk.set_defaults(func=command_verify_chunk)

    fault = sub.add_parser("fault-chunk", help="test-only persistence fault injection")
    fault.add_argument("--state-dir", default="artifacts/state")
    fault.add_argument("--chunk-index", type=int, required=True)
    fault.add_argument("--mode", choices=["digest", "truncate", "frame-count", "delete"], default="digest")
    fault.add_argument("--delta", type=float, default=0.125)
    fault.set_defaults(func=command_fault_chunk)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AnimRecalcError as exc:
        print_json(exc.to_json())
        return EXIT_ERROR
    except FileNotFoundError as exc:
        error = AnimRecalcError("E_IO", str(exc))
        print_json(error.to_json())
        return EXIT_ERROR
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        error = AnimRecalcError(
            "E_SCHEMA",
            f"malformed input: {type(exc).__name__}: {exc}",
            {"exception_type": type(exc).__name__},
        )
        print_json(error.to_json())
        return EXIT_ERROR
    except Exception as exc:  # Final JSON boundary for callers/tests.
        error = AnimRecalcError(
            "E_INTERNAL",
            f"{type(exc).__name__}: {exc}",
            {"exception_type": type(exc).__name__},
        )
        print_json(error.to_json())
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
