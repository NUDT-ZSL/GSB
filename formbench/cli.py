from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Sequence

from . import EXIT_INTERRUPTED, EXIT_PARTIAL, EXIT_SCHEMA
from .errors import FormBenchError, InterruptedBySignal
from .imaging import read_pgm
from .runner import RunOptions, Workbench, preflight_task
from .schema import load_task, load_truth
from .scheduler import StopController


def _print_error(error: FormBenchError) -> None:
    print(json.dumps({"ok": False, "error": error.to_dict()}, ensure_ascii=True, indent=2), file=sys.stderr)


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _install_signal_handlers(stop: StopController) -> None:
    def handler(signum: int, _frame: object) -> None:
        stop.request_stop(reason="termination_signal", signum=signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Not the main thread or signal unsupported on this platform.
            pass


def _cmd_validate(args: argparse.Namespace) -> int:
    task = load_task(args.task)
    issues = []
    for spec in task.images:
        try:
            image = read_pgm(spec.path)
            expected_w = spec.width or task.image_width
            expected_h = spec.height or task.image_height
            if image.shape != (expected_h, expected_w):
                issues.append(
                    {
                        "image_id": spec.image_id,
                        "code": "E_IMAGE_DIMENSIONS",
                        "field": str(spec.path),
                        "message": f"expected {expected_w}x{expected_h}, got {image.shape[1]}x{image.shape[0]}",
                    }
                )
            load_truth(spec.truth_path)
        except FormBenchError as exc:
            issues.append(exc.to_dict())
    print(json.dumps({"ok": not issues, "task_id": task.task_id, "issues": issues}, indent=2))
    return 0 if not issues else 3


def _cmd_run(args: argparse.Namespace) -> int:
    task = load_task(args.task)
    preflight_task(task)
    stop = StopController()
    _install_signal_handlers(stop)
    options = RunOptions(
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        concurrency=args.concurrency,
        stop_after_blocks=args.stop_after_blocks,
    )
    with Workbench(task, options=options, stop=stop) as bench:
        try:
            summary = bench.run()
        except InterruptedBySignal as exc:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "code": "E_INTERRUPTED",
                        "exit_code": EXIT_INTERRUPTED,
                        "error": exc.to_dict(),
                    },
                    ensure_ascii=True,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return EXIT_INTERRUPTED
    print(
        json.dumps(
            {
                "ok": summary["error_count"] == 0,
                "task_id": summary["task_id"],
                "version_id": summary["version_id"],
                "form_count": summary["form_count"],
                "error_count": summary["error_count"],
                "results": str((args.output_dir or task.output_dir) / "results.json"),
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0 if summary["error_count"] == 0 else EXIT_PARTIAL


def _cmd_seed(args: argparse.Namespace) -> int:
    from .generator import generate_dataset

    task = generate_dataset(args.root)
    print(json.dumps({"generated": len(task["images"]), "root": str(args.root.resolve())}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="formbench", description="Deterministic form digitization workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run detection, recognition, ledger and outputs")
    run.add_argument("--task", type=Path, default=Path("samples/task.json"))
    run.add_argument("--state-dir", type=Path)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--concurrency", type=_positive_int)
    run.add_argument("--stop-after-blocks", type=_positive_int)
    run.set_defaults(handler=_cmd_run)

    validate = sub.add_parser("validate", help="validate task, images and truth without writing files")
    validate.add_argument("--task", type=Path, default=Path("samples/task.json"))
    validate.set_defaults(handler=_cmd_validate)

    seed = sub.add_parser("seed", help="generate deterministic samples")
    seed.add_argument("--root", type=Path, default=Path.cwd())
    seed.set_defaults(handler=_cmd_seed)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except FormBenchError as exc:
        _print_error(exc)
        return int(exc.exit_code if isinstance(exc.exit_code, int) else EXIT_SCHEMA)


if __name__ == "__main__":
    raise SystemExit(main())
