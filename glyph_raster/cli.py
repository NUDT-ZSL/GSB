from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import traceback
from typing import Any

from .engine import RasterEngine
from .errors import EngineError
from .model import load_scene_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glyph-raster",
        description="Deterministic incremental vector glyph rasterizer",
    )
    parser.add_argument("--version", action="version", version="glyph-raster 1.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("render", "render or incrementally resume a scene"),
        ("resume", "resume persisted tile state (alias of render)"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--scene", required=True, help="scene JSON path")
        command.add_argument(
            "--workspace",
            default=".",
            help="workspace root; scene-relative paths resolve below it",
        )
        edit_group = command.add_mutually_exclusive_group()
        edit_group.add_argument(
            "--apply-edits",
            dest="apply_edits",
            action="store_true",
            default=True,
            help="apply the scene edit script (default)",
        )
        edit_group.add_argument(
            "--base",
            "--no-edits",
            dest="apply_edits",
            action="store_false",
            help="render the pre-edit base outline, useful before incremental updates",
        )

    verify = subparsers.add_parser(
        "verify", help="verify tiles, bitmap, report and optionally full rasterization"
    )
    verify.add_argument("--scene", required=True, help="scene JSON path")
    verify.add_argument(
        "--workspace",
        default=".",
        help="workspace root; scene-relative paths resolve below it",
    )
    verify_edit_group = verify.add_mutually_exclusive_group()
    verify_edit_group.add_argument(
        "--apply-edits",
        dest="apply_edits",
        action="store_true",
        default=True,
        help="verify the scene with its edit script applied (default)",
    )
    verify_edit_group.add_argument(
        "--base",
        "--no-edits",
        dest="apply_edits",
        action="store_false",
        help="verify the pre-edit base outline",
    )
    verify.add_argument(
        "--full-check",
        action="store_true",
        help="re-rasterize every tile serially and compare against persisted assembly",
    )
    return parser


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    sys.stdout.flush()


def _run_render(args: argparse.Namespace) -> int:
    bundle = load_scene_bundle(
        args.scene,
        workspace=args.workspace,
        apply_edits=args.apply_edits,
    )
    engine = RasterEngine(bundle)
    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda number, frame: engine.request_stop(number))
    try:
        result = engine.render()
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    _emit(result)
    return int(result.get("exitCode", 0))


def _run_verify(args: argparse.Namespace) -> int:
    bundle = load_scene_bundle(
        args.scene,
        workspace=args.workspace,
        apply_edits=args.apply_edits,
    )
    engine = RasterEngine(bundle)
    result = engine.verify(full_check=args.full_check)
    _emit(result)
    return int(result.get("exitCode", 0))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ("render", "resume"):
            return _run_render(args)
        if args.command == "verify":
            return _run_verify(args)
        raise EngineError("E_INTERNAL", f"unknown command: {args.command}")
    except EngineError as exc:
        _emit(exc.as_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        error = EngineError("E_INTERRUPTED", "rendering interrupted")
        _emit(error.as_dict())
        return 130
    except Exception as exc:  # pragma: no cover - last-resort guard
        if os.environ.get("GLYPH_RASTER_DEBUG"):
            traceback.print_exc()
        error = EngineError(
            "E_INTERNAL", f"unexpected internal error: {exc}", {"type": type(exc).__name__}
        )
        _emit(error.as_dict())
        return error.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
