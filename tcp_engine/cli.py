from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from .errors import E_DB, EngineError
from .runner import (
    build_report,
    resume_scene,
    run_scene,
    status_report,
    verify_scene,
)
from .scene import load_scene
from .store import Store


def _add_scene_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scene", help="combined scene JSON")
    parser.add_argument("--link", help="link JSON")
    parser.add_argument("--script", help="connection script JSON")
    parser.add_argument("--congestion", help="congestion-control JSON")
    parser.add_argument("--observations", help="observation JSON")
    parser.add_argument("--name", help="scene display name override")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcp-trajectory",
        description="Deterministic local TCP state and congestion-control engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start a new trajectory version")
    _add_scene_args(run)
    run.add_argument("--db", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--segment-start", type=int)
    run.add_argument("--segment-end", type=int)
    run.add_argument("--version-tag")
    run.add_argument("--pretty", action="store_true")

    resume = subparsers.add_parser("resume", help="resume the latest incomplete version")
    _add_scene_args(resume)
    resume.add_argument("--db", required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--segment-end", type=int)
    resume.add_argument("--pretty", action="store_true")

    status = subparsers.add_parser("status", help="show persisted run status")
    status.add_argument("--db", required=True)
    status.add_argument("--run-id", required=True)
    status.add_argument("--pretty", action="store_true")

    verify = subparsers.add_parser("verify", help="replay and verify active trace")
    _add_scene_args(verify)
    verify.add_argument("--db", required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--pretty", action="store_true")

    report = subparsers.add_parser("report", help="emit a stable trajectory report")
    report.add_argument("--db", required=True)
    report.add_argument("--run-id", required=True)
    report.add_argument("--pretty", action="store_true")
    return parser


def _scene_from_args(args: argparse.Namespace):
    return load_scene(
        link_path=args.link,
        script_path=args.script,
        congestion_path=args.congestion,
        observations_path=args.observations,
        scene_path=args.scene,
        name=args.name,
    )


def _emit(value: dict[str, Any], pretty: bool) -> None:
    if pretty:
        text = json.dumps(
            value, sort_keys=True, indent=2, ensure_ascii=False
        )
    else:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "status":
        store = Store(args.db)
        try:
            return status_report(store=store, run_id=args.run_id)
        finally:
            store.close()
    if args.command == "report":
        store = Store(args.db)
        try:
            return build_report(store=store, run_id=args.run_id)
        finally:
            store.close()

    scene = _scene_from_args(args)
    store = Store(args.db)
    try:
        if args.command == "run":
            return run_scene(
                store=store,
                run_id=args.run_id,
                scene=scene,
                segment_start=args.segment_start,
                segment_end=args.segment_end,
                version_tag=args.version_tag,
            )
        if args.command == "resume":
            return resume_scene(
                store=store,
                run_id=args.run_id,
                scene=scene,
                segment_end=args.segment_end,
            )
        if args.command == "verify":
            return verify_scene(store=store, run_id=args.run_id, scene=scene)
    finally:
        store.close()
    raise EngineError(E_DB, f"unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _execute(args)
        _emit(result, bool(getattr(args, "pretty", False)))
        return 0
    except EngineError as exc:
        sys.stderr.write(
            json.dumps(
                exc.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.stderr.flush()
        return 2
    except Exception as exc:  # pragma: no cover - last-resort diagnostics
        details: dict[str, Any] = {"type": type(exc).__name__}
        if os_environ_debug():
            details["traceback"] = traceback.format_exc()
        sys.stderr.write(
            json.dumps(
                {"error": {"code": "E_INTERNAL", "message": str(exc), "details": details}},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            + "\n"
        )
        sys.stderr.flush()
        return 3


def os_environ_debug() -> bool:
    import os

    return os.environ.get("TCP_ENGINE_DEBUG") == "1"


if __name__ == "__main__":
    raise SystemExit(main())

