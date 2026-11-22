from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from . import __version__
from .engine import WorkbenchEngine
from .errors import BusinessError, SimulatedCrash


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cdn-workbench",
        description="Deterministic local CDN edge cache consistency workbench",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="load samples and execute one consistency round")
    run.add_argument("--samples", default="samples")
    run.add_argument("--state", default="state")
    run.add_argument("--logical-clock", type=int, default=None)
    run.add_argument("--run-id", default=None)
    run.add_argument(
        "--fault-point",
        choices=["after_prepare", "during_apply"],
        default=None,
    )
    run.add_argument("--origin-url", default=None)
    run.add_argument("--fail-on-inconsistency", action="store_true")
    run.add_argument("--compact", action="store_true")
    run.add_argument("--debug", action="store_true")

    recover = subparsers.add_parser("recover", help="replay prepared runs and clean orphans")
    recover.add_argument("--state", default="state")
    recover.add_argument("--compact", action="store_true")

    inspect = subparsers.add_parser("inspect", help="print the committed SQLite snapshot")
    inspect.add_argument("--state", default="state")
    inspect.add_argument("--compact", action="store_true")
    return parser


def _emit(value: Any, compact: bool) -> None:
    if compact:
        print(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    else:
        print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            engine = WorkbenchEngine(
                samples_dir=arguments.samples,
                state_dir=arguments.state,
                origin_base_url=arguments.origin_url,
            )
            report = engine.run(
                logical_clock=arguments.logical_clock,
                run_id=arguments.run_id,
                fault_point=arguments.fault_point,
            )
            _emit(report, arguments.compact)
            if arguments.fail_on_inconsistency and report.get("audits"):
                return 3
            return 0
        if arguments.command == "recover":
            engine = WorkbenchEngine(samples_dir="samples", state_dir=arguments.state)
            _emit(engine.recover(), arguments.compact)
            return 0
        if arguments.command == "inspect":
            engine = WorkbenchEngine(samples_dir="samples", state_dir=arguments.state)
            _emit(engine.inspect(), arguments.compact)
            return 0
        parser.error("unknown command")
        return 2
    except BusinessError as error:
        _emit(error.to_dict(), getattr(arguments, "compact", False))
        return 2
    except SimulatedCrash as error:
        _emit(
            {
                "ok": False,
                "simulated_crash": True,
                "message": str(error),
                "fault_point": arguments.fault_point,
                "state": arguments.state,
            },
            getattr(arguments, "compact", False),
        )
        return 70
    except KeyboardInterrupt:
        _emit({"ok": False, "interrupted": True}, getattr(arguments, "compact", False))
        return 130
    except Exception as error:
        if getattr(arguments, "debug", False):
            traceback.print_exc(file=sys.stderr)
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "E9999_UNEXPECTED",
                    "message": str(error),
                    "type": type(error).__name__,
                },
            },
            getattr(arguments, "compact", False),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

