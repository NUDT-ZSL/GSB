from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .errors import InputError, WorkbenchError
from .jsonio import canonical_json_bytes
from .runner import RunnerConfig, WorkbenchRunner
from .scheduler import InterruptController
from .validation import load_bundle, validate_paths


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", required=True, help="state declaration JSON")
    parser.add_argument("--story", required=True, help="story JSON")
    parser.add_argument("--storyboard", required=True, help="storyboard JSON")
    parser.add_argument("--run", dest="run_config", help="runtime choices JSON")


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", required=True, help="snapshot and recovery directory")
    parser.add_argument("--output-dir", required=True, help="batch output directory")
    parser.add_argument("--workers", type=int, default=2, help="bounded worker count")
    parser.add_argument(
        "--memory-budget-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="total in-flight state/frame buffer budget",
    )
    parser.add_argument(
        "--frame-block-size", type=int, default=4, help="frames per persisted block"
    )
    parser.add_argument("--fresh", action="store_true", help="archive current run and restart")
    parser.add_argument(
        "--fault-after-boundary",
        type=int,
        default=None,
        help="test-only: interrupt after this many persisted boundaries",
    )
    parser.add_argument(
        "--fault-signal",
        choices=("sigint", "sigterm", "sigkill"),
        default="sigint",
        help="test-only signal associated with --fault-after-boundary",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress on stderr")


def _print_progress(event: dict[str, Any]) -> None:
    kind = event.get("event")
    if kind == "recovery":
        report = event["report"]
        print(
            "recovery:",
            f"sim_seq={report['sim']['used_sequence']}",
            f"blocks={report['frames']['valid_block_count']}",
            f"digest_errors={len(report['errors'])}",
            file=sys.stderr,
        )
    elif kind == "simulation-boundary":
        record = event.get("record") or {}
        print(
            "event-boundary:",
            f"record={event.get('ledger_count', 0)}",
            f"scene={record.get('scene_id')}",
            f"event={record.get('event_id') or record.get('branch_id') or record.get('kind')}",
            f"snapshot={event.get('snapshot_sequence')}",
            file=sys.stderr,
        )
    elif kind == "frame-block-ready":
        print(
            "frame-block:",
            event.get("unit_key"),
            f"frames={len(event.get('frames', []))}",
            file=sys.stderr,
        )
    elif kind == "interrupted":
        print("interrupted at persisted boundary", file=sys.stderr)
    elif kind == "complete":
        print("complete", file=sys.stderr)
    elif kind == "archive":
        print(f"archived: {event.get('source')} -> {event.get('destination')}", file=sys.stderr)


def _configure_signals(controller: InterruptController) -> None:
    def handler(signum: int, _frame: Any) -> None:
        if signum == signal.SIGINT:
            controller.request("SIGINT", 130)
        elif signum == signal.SIGTERM:
            controller.request("SIGTERM", 143)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def _error_payload(exc: WorkbenchError) -> dict[str, Any]:
    return exc.to_dict()


def command_validate(args: argparse.Namespace) -> int:
    result = validate_paths(
        state_path=args.state,
        story_path=args.story,
        storyboard_path=args.storyboard,
        run_path=args.run_config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_run(args: argparse.Namespace) -> int:
    bundle = load_bundle(
        state_path=args.state,
        story_path=args.story,
        storyboard_path=args.storyboard,
        run_path=args.run_config,
    )
    config = RunnerConfig(
        workers=args.workers,
        memory_budget_bytes=args.memory_budget_bytes,
        frame_block_size=args.frame_block_size,
        fresh=args.fresh,
        fault_after_boundary=args.fault_after_boundary,
        fault_signal=args.fault_signal,
    )
    controller = InterruptController()
    _configure_signals(controller)
    runner = WorkbenchRunner(
        bundle,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        config=config,
        interrupt=controller,
        progress=None if args.quiet else _print_progress,
    )
    try:
        result = runner.run()
    except WorkbenchError as exc:
        print(
            json.dumps(_error_payload(exc), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return exc.exit_code
    print(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    )
    return 0


def command_ui(args: argparse.Namespace) -> int:
    bundle = load_bundle(
        state_path=args.state,
        story_path=args.story,
        storyboard_path=args.storyboard,
        run_path=args.run_config,
    )
    if args.offscreen:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from .ui.app import run_ui

    return run_ui(
        bundle,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        config=RunnerConfig(
            workers=args.workers,
            memory_budget_bytes=args.memory_budget_bytes,
            frame_block_size=args.frame_block_size,
        ),
        smoke_test=args.smoke_test,
    )


def command_result(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        raise InputError("E_JSON", f"result file does not exist: {path}", {"path": str(path)})
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(
            "E_JSON", f"cannot read result file: {path}", {"reason": str(exc)}
        ) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="narrative-workbench",
        description="Deterministic interactive-narrative workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="parse and validate all local inputs without writing state"
    )
    _add_input_arguments(validate_parser)
    validate_parser.set_defaults(handler=command_validate)

    run_parser = subparsers.add_parser(
        "run", help="run or resume deterministic simulation and storyboard replay"
    )
    _add_input_arguments(run_parser)
    _add_runtime_arguments(run_parser)
    run_parser.set_defaults(handler=command_run)

    ui_parser = subparsers.add_parser("ui", help="open the PySide6 desktop workbench")
    _add_input_arguments(ui_parser)
    _add_runtime_arguments(ui_parser)
    ui_parser.add_argument(
        "--offscreen", action="store_true", help="force the Qt offscreen platform plugin"
    )
    ui_parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="open and close the window automatically",
    )
    ui_parser.set_defaults(handler=command_ui)

    result_parser = subparsers.add_parser("result", help="print a completed result JSON")
    result_parser.add_argument("path")
    result_parser.set_defaults(handler=command_result)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except WorkbenchError as exc:
        print(
            json.dumps(_error_payload(exc), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return exc.exit_code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

