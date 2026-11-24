"""Command line interface for generation, processing and verification."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import PipelineError
from .generator import generate_sample
from .pipeline import PipelineConfig, run_pipeline
from .storage import GRACEFUL_EXIT_CODE


def _emit(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    sys.stdout.write("\n")


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def handler(signum: int, frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="surface-stitch",
        description="Local deterministic surface tile registration and stitching",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--output", default="samples/generated")

    run = sub.add_parser("run", help="validate, register, arbitrate, fuse and export")
    run.add_argument("--input", "--sample-dir", dest="sample_dir", default="samples/generated")
    run.add_argument("--output-dir", default="outputs/default")
    run.add_argument("--state-dir", default="state/default")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--max-inflight-pixels", type=int, default=None)
    run.add_argument("--max-spectrum-mb", type=int, default=None)
    run.add_argument("--interrupt-after", type=int, default=None,
                     help="test hook: request graceful stop after N new tiles")
    run.add_argument("--no-serial-check", action="store_true")
    run.add_argument("--no-publish", action="store_true")

    verify = sub.add_parser("verify", help="validate inputs and cached block digests")
    verify.add_argument("--input", "--sample-dir", dest="sample_dir", default="samples/generated")
    verify.add_argument("--state-dir", default="state/default")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            project = generate_sample(Path(args.output))
            _emit({"ok": True, "output": str(Path(args.output).resolve()), "project": project})
            return 0

        if args.command == "verify":
            from .loader import load_project
            from .storage import verify_tile

            sample = load_project(args.sample_dir)
            results = []
            for tile in sample.tiles:
                try:
                    _stored, check = verify_tile(
                        Path(args.state_dir),
                        tile["tile_id"],
                        int(tile["width"]),
                        int(tile["height"]),
                        sample.version_id,
                    )
                except PipelineError as exc:
                    check = {
                        "tile_id": tile["tile_id"],
                        "valid": False,
                        "code": exc.code,
                        "message": exc.message,
                    }
                results.append(check)
            _emit({
                "ok": all(
                    item.get("valid") is True or item.get("status") == "missing"
                    for item in results
                ),
                "version_id": sample.version_id,
                "checks": results,
            })
            return 0

        stop_event = threading.Event()
        _install_signal_handlers(stop_event)
        config = PipelineConfig(
            sample_dir=Path(args.sample_dir),
            output_dir=Path(args.output_dir),
            state_dir=Path(args.state_dir),
            workers=max(1, int(args.workers or 2)),
            max_inflight_pixels=args.max_inflight_pixels,
            max_spectrum_bytes=(
                args.max_spectrum_mb * 1024 * 1024 if args.max_spectrum_mb else None
            ),
            check_serial=not args.no_serial_check,
            publish=not args.no_publish,
            interrupt_after=args.interrupt_after,
            stop_event=stop_event,
        )
        report = run_pipeline(config)
        if report.get("interrupted"):
            _emit({
                "ok": False,
                "interrupted": True,
                "exit_code": GRACEFUL_EXIT_CODE,
                "state_dir": str(config.state_dir.resolve()),
                "completed_tiles": report.get("completed_tiles"),
                "pending_tiles": report.get("pending_tiles"),
            })
            return GRACEFUL_EXIT_CODE
        _emit({
            "ok": True,
            "version_id": report["version_id"],
            "report_path": str((config.output_dir / config.report_name).resolve()),
            "image_path": str((config.output_dir / config.image_name).resolve()),
            "registration_summary": report["registration_summary"],
            "conflict_count": report["conflict_count"],
            "fusion_consistency": report["fusion_consistency"],
        })
        return 0
    except PipelineError as exc:
        _emit(exc.to_dict())
        return 2
    except FileNotFoundError as exc:
        error = PipelineError("E_MANIFEST", f"file not found: {exc}")
        _emit(error.to_dict())
        return 2
    except Exception as exc:  # last-resort machine-readable boundary
        error = PipelineError("E_INTERNAL", f"unexpected pipeline failure: {exc}")
        _emit(error.to_dict())
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
