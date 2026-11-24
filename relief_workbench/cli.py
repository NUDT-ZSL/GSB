from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sys
from typing import Any, Callable

from .config import load_config
from .engine import WorkbenchEngine
from .errors import WorkbenchError
from .sample import generate_sample


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state",
        default=".relief-state",
        help="writable state/cache directory (default: .relief-state)",
    )
    parser.add_argument(
        "--output",
        default="out",
        help="result output directory (default: out)",
    )


def _install_signal_handlers(engine: WorkbenchEngine) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        engine.request_termination(signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        try:
            previous[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, handler)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signal_number, old_handler in previous.items():
        try:
            signal.signal(signal_number, old_handler)
        except (OSError, ValueError):
            pass


def _engine_from_args(args: argparse.Namespace) -> WorkbenchEngine:
    config = load_config(args.config)
    return WorkbenchEngine(
        config,
        state_dir=args.state,
        output_dir=args.output,
        tile_delay_seconds=max(0.0, float(args.tile_delay_ms) / 1000.0),
    )


def _run_guarded(action: Callable[[], dict[str, Any]]) -> int:
    try:
        payload = action()
    except WorkbenchError as exc:
        _json_print(exc.as_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        _json_print(
            WorkbenchError(
                "E_INTERRUPTED",
                "interrupted before an engine checkpoint",
                {"signal": int(signal.SIGINT)},
                exit_code=130,
            ).as_dict()
        )
        return 130
    _json_print(payload)
    return 0


def _cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    return {
        "ok": True,
        "version": config.version,
        "config": config.public_dict(),
    }


def _cmd_sample(args: argparse.Namespace) -> dict[str, Any]:
    return generate_sample(args.directory)


def _cmd_preview(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine_from_args(args)
    previous = _install_signal_handlers(engine)
    try:
        result = engine.preview_tile(args.row, args.col)
    finally:
        _restore_signal_handlers(previous)
    return {
        "ok": True,
        "version": engine.version,
        "tile": result.spec.to_dict(),
        "cache_hit": result.cache_hit,
        "pixel_sha256": hashlib.sha256(result.pixels.tobytes()).hexdigest(),
        "output_path": str(result.output_path),
    }


def _cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine_from_args(args)
    previous = _install_signal_handlers(engine)
    try:
        return engine.render_all()
    finally:
        _restore_signal_handlers(previous)


def _cmd_gui(args: argparse.Namespace) -> dict[str, Any]:
    from .gui import launch_gui

    launch_gui(
        config_path=args.config,
        state_dir=args.state,
        output_dir=args.output,
    )
    return {"ok": True, "message": "GUI closed"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relief-workbench",
        description="Deterministic procedural relief tiling workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a JSON config")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=_cmd_validate)

    sample = subparsers.add_parser(
        "sample", help="generate fixed local sample config and truth"
    )
    sample.add_argument("--dir", dest="directory", default="samples")
    sample.set_defaults(func=_cmd_sample)

    preview = subparsers.add_parser("preview", help="render and cache one tile")
    preview.add_argument("--config", required=True)
    _common_paths(preview)
    preview.add_argument("--row", type=int)
    preview.add_argument("--col", type=int)
    preview.add_argument("--tile-delay-ms", type=float, default=0.0, help=argparse.SUPPRESS)
    preview.set_defaults(func=_cmd_preview)

    render = subparsers.add_parser(
        "render", help="render remaining tiles, verify, and write report"
    )
    render.add_argument("--config", required=True)
    _common_paths(render)
    render.add_argument("--tile-delay-ms", type=float, default=0.0, help=argparse.SUPPRESS)
    render.set_defaults(func=_cmd_render)

    gui = subparsers.add_parser("gui", help="open the local desktop workbench")
    gui.add_argument("--config")
    _common_paths(gui)
    gui.set_defaults(func=_cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return _run_guarded(lambda: args.func(args))
