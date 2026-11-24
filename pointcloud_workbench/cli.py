"""Command-line interface for generation, rendering, viewing, and acceptance."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from .acceptance import run_acceptance
from .errors import EXIT_INTERNAL, BusinessError, InterruptedRun
from .pipeline import RenderOptions, render_task
from .samplegen import generate_samples
from .viewer import build_static_viewer, launch_tk_viewer


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - exercised via CLI
        raise BusinessError("E_ARGUMENT", message)


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, handler)
        except (ValueError, OSError):
            pass


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="pcwb", description="Offline deterministic point-cloud block workbench")
    parser.add_argument("--debug", action="store_true", help="print tracebacks for internal failures")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--out", required=True, type=Path, help="sample output directory")

    render = subparsers.add_parser("render", help="render a task with block caching")
    render.add_argument("--task", required=True, type=Path)
    render.add_argument("--state", required=True, type=Path, help="persistent block-cache directory")
    render.add_argument("--output", required=True, type=Path)
    render.add_argument(
        "--verify-full",
        action="store_true",
        help="compare every camera against an independent serial whole-image recomputation",
    )

    view = subparsers.add_parser("view", help="open diagnostics from persisted results")
    view.add_argument("--output", required=True, type=Path)
    view.add_argument("--gui", action="store_true", help="launch local Tk inspector instead of only HTML")
    view.add_argument("--camera", help="initial camera tab for the Tk inspector")

    selftest = subparsers.add_parser("selftest", help="run the complete local acceptance workflow")
    selftest.add_argument("--workdir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    debug = "--debug" in (argv if argv is not None else sys.argv[1:])
    try:
        parser = _parser()
        args = parser.parse_args(argv)
        debug = bool(args.debug)
        if args.command == "generate":
            manifest = generate_samples(args.out)
            _emit(
                {
                    "ok": True,
                    "out": str(Path(args.out).resolve()),
                    "manifest": str((Path(args.out) / "sample_manifest.json").resolve()),
                    "base_dataset_digest": manifest["base_dataset_digest"],
                    "edited_dataset_digest": manifest["edited_dataset_digest"],
                }
            )
            return 0
        if args.command == "render":
            stop_event = threading.Event()
            _install_signal_handlers(stop_event)
            report = render_task(
                args.task,
                RenderOptions(
                    state_root=args.state,
                    output_root=args.output,
                    verify_full=args.verify_full,
                    stop_event=stop_event,
                ),
            )
            _emit(
                {
                    "ok": True,
                    "report": str((Path(args.output) / "render_report.json").resolve()),
                    "viewer": report["viewer"],
                    "cameras": [
                        {
                            "camera_id": camera["camera_id"],
                            "rendered_blocks": camera["rendered_block_count"],
                            "reused_blocks": camera["reused_block_count"],
                            "invalidated_blocks": camera["invalidated_block_count"],
                            "full_recompute_verified": camera["full_recompute_verification"].get(
                                "pixel_identical"
                            ),
                        }
                        for camera in report["cameras"]
                    ],
                }
            )
            return 0
        if args.command == "view":
            viewer = build_static_viewer(args.output)
            if args.gui:
                launch_tk_viewer(args.output, args.camera)
            _emit({"ok": True, "viewer": str(viewer), "gui": bool(args.gui)})
            return 0
        if args.command == "selftest":
            result = run_acceptance(args.workdir)
            _emit(
                {
                    "ok": result["ok"],
                    "report": str((Path(args.workdir) / "acceptance_report.json").resolve()),
                    "checks": result["checks"],
                }
            )
            return 0 if result["ok"] else EXIT_INTERNAL
        raise BusinessError("E_ARGUMENT", "unknown command")
    except InterruptedRun as exc:
        _emit(exc.to_dict())
        return exc.exit_code
    except BusinessError as exc:
        _emit(exc.to_dict())
        if debug:
            import traceback

            traceback.print_exc()
        return exc.exit_code
    except KeyboardInterrupt:
        _emit(
            {
                "ok": False,
                "interrupted": True,
                "error": {"code": "E_INTERRUPTED", "message": "keyboard interrupt", "details": {}},
                "exit_code": 130,
            }
        )
        return 130
    except Exception as exc:  # pragma: no cover - last-resort process boundary
        error = BusinessError(
            "E_INTERNAL",
            "unexpected internal error",
            details={"exception_type": type(exc).__name__, "exception": str(exc)},
            exit_code=EXIT_INTERNAL,
        )
        _emit(error.to_dict())
        if debug:
            import traceback

            traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

