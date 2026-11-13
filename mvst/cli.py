"""Command line interface: validate / package / info / selftest.

Exit code contract::

    0   success
    2   input or business error (single-line JSON on stderr, code starts E_)
    3   internal error
    130 interrupted by Ctrl+C / SIGBREAK
"""

from __future__ import annotations

import argparse
import json
import signal
import sys

from . import __version__
from .container import (
    ENTRY_SIZE, HEADER_SIZE, TYPE_AUDIO,
    TYPE_SUBTITLE, TYPE_VIDEO, code_language, read_master_header,
)
from .errors import MvstError, emit_error
from .pipeline import REPORT_KEY_ORDER, run_pipeline

_INTERRUPTED = False


def _install_signal_handlers() -> None:
    def _handler(signum: int, _frame: object) -> None:
        global _INTERRUPTED
        _INTERRUPTED = True
        # Temp files created during packaging are removed by the container
        # layer's BaseException cleanup; here we also sweep stragglers.
        raise KeyboardInterrupt

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass


def _ordered_report(report: dict) -> dict:
    return {key: report[key] for key in REPORT_KEY_ORDER if key in report}


def _cmd_validate(args: argparse.Namespace) -> int:
    report, _code = run_pipeline(args.manifest, args.root, args.jobs)
    sys.stdout.write(json.dumps(_ordered_report(report), ensure_ascii=False,
                                indent=2, sort_keys=False) + "\n")
    return 0


def _cmd_package(args: argparse.Namespace) -> int:
    report, _code = run_pipeline(
        args.manifest, args.root, args.jobs,
        package_out=args.out, force=args.force,
    )
    sys.stdout.write(json.dumps(_ordered_report(report), ensure_ascii=False,
                                indent=2, sort_keys=False) + "\n")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    info = read_master_header(args.master)
    type_name = {TYPE_VIDEO: "video", TYPE_AUDIO: "audio",
                 TYPE_SUBTITLE: "subtitle"}
    payload = {
        "file": str(args.master),
        "magic": "MVST",
        "version": info.version,
        "master_sample_rate": info.sample_rate,
        "video_frames": info.video_frames,
        "audio_track_count": info.audio_track_count,
        "subtitle_track_count": info.subtitle_track_count,
        "header_size": HEADER_SIZE,
        "entry_size": ENTRY_SIZE,
        "index_only": True,
        "tracks": [
            {
                "type": type_name.get(entry.track_type, "unknown"),
                "track_number": entry.track_number,
                "language": code_language(entry.language_code),
                "offset_ms": entry.offset_ms,
                "rate": entry.sample_rate,
                "byte_offset": entry.byte_offset,
                "length": entry.data_length,
                "start_unit": entry.start_unit,
                "end_unit": entry.end_unit,
            }
            for entry in info.entries
        ],
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def _cmd_selftest(_args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    return run_selftest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvst",
        description="Deterministic multi-language mastering container tool",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest", required=True)
    common.add_argument("--root", required=True)
    common.add_argument("--jobs", type=int, default=1)

    p_validate = sub.add_parser("validate", parents=[common])
    p_validate.set_defaults(func=_cmd_validate)

    p_package = sub.add_parser("package", parents=[common])
    p_package.add_argument("--out", required=True)
    p_package.add_argument("--force", action="store_true")
    p_package.set_defaults(func=_cmd_package)

    p_info = sub.add_parser("info")
    p_info.add_argument("master")
    p_info.set_defaults(func=_cmd_info)

    p_self = sub.add_parser("selftest")
    p_self.set_defaults(func=_cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return args.func(args)
    except MvstError as exc:
        if exc.report is not None:
            sys.stdout.write(json.dumps(_ordered_report(exc.report),
                                        ensure_ascii=False, indent=2,
                                        sort_keys=False) + "\n")
            sys.stdout.flush()
        emit_error(exc.error_code, **exc.fields)
        return 2
    except KeyboardInterrupt:
        if not _INTERRUPTED:
            emit_error("E_INTERRUPTED")
        return 130
    except (BrokenPipeError,):  # pragma: no cover
        return 3
    except Exception as exc:  # noqa: BLE001 - never leak a traceback
        emit_error("E_INTERNAL", detail=type(exc).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main())
