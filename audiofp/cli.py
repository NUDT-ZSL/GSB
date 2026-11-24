from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

from .config import load_task
from .errors import AudioFPError, TaskInterrupted
from .pipeline import StopController, run_search


def _print_json(value: Any) -> None:
    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    sys.stdout.flush()


def _error_exit(exc: AudioFPError) -> int:
    _print_json(exc.to_dict())
    return 2


def _run(args: argparse.Namespace) -> int:
    stop = StopController()
    previous_handlers: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        stop.request(signum)

    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signal_number] = signal.signal(signal_number, handler)
        except (ValueError, OSError):
            pass

    def progress(event: str, data: dict[str, Any]) -> None:
        if args.verbose:
            sys.stderr.write(
                json.dumps(
                    {"event": event, **data},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
            sys.stderr.flush()

    try:
        report = run_search(
            Path(args.task),
            force_full=args.force_full,
            stop=stop,
            progress=progress if args.verbose else None,
        )
        _print_json(
            {
                "ok": True,
                "report": report["files"]["report"],
                "segments": report["files"]["segments"],
                "summary": report["summary"],
                "unmatched_queries": report["unmatched_queries"],
                "affected_interval_count": len(
                    report["incremental"]["affected_intervals"]
                ),
            }
        )
        return 0
    except TaskInterrupted as exc:
        _print_json(
            {
                "ok": False,
                "interrupted": True,
                "signal": exc.signal_name,
                "exit_code": exc.exit_code,
            }
        )
        return exc.exit_code
    except AudioFPError as exc:
        return _error_exit(exc)
    except Exception as exc:
        return _error_exit(
            AudioFPError(
                "E_INTERNAL",
                "检索过程中发生未分类异常",
                error=str(exc),
                exception_type=type(exc).__name__,
            )
        )
    except KeyboardInterrupt:
        _print_json(
            {
                "ok": False,
                "interrupted": True,
                "signal": "SIGINT",
                "exit_code": 130,
            }
        )
        return 130
    finally:
        for signal_number, previous in previous_handlers.items():
            try:
                signal.signal(signal_number, previous)
            except (ValueError, OSError):
                pass


def _validate(args: argparse.Namespace) -> int:
    try:
        config = load_task(Path(args.task))
    except AudioFPError as exc:
        return _error_exit(exc)
    _print_json(
        {
            "ok": True,
            "task": str(config.task_path),
            "reference_count": len(config.references),
            "query_count": len(config.queries),
            "sample_rates": sorted(
                {track.info.sample_rate for track in config.all_tracks}
            ),
            "fingerprint_version": config.fingerprint.version,
            "search_version": config.search.version,
            "state_dir": str(config.state_dir),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本地 WAV 指纹检索与版权比对工具"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行检索")
    run_parser.add_argument("--task", required=True, help="检索任务 JSON")
    run_parser.add_argument(
        "--force-full",
        action="store_true",
        help="忽略有效缓存并从头重算",
    )
    run_parser.add_argument(
        "--verbose", action="store_true", help="向 stderr 输出分块进度"
    )
    run_parser.set_defaults(func=_run)

    validate_parser = subparsers.add_parser(
        "validate", help="仅校验任务与 WAV 头，不创建状态目录"
    )
    validate_parser.add_argument("--task", required=True, help="检索任务 JSON")
    validate_parser.set_defaults(func=_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

