"""Command line entry points for the differentiable rendering pipeline."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from typing import Dict, Optional

from .errors import PipelineError, render_error
from .pipeline import run_pipeline
from .schema import load_config, resolve_output_paths

# Conventional process exit codes for handled termination signals.
SIGNAL_EXIT_CODES = {
    signal.SIGINT: 130,
    signal.SIGTERM: 143,
}


def _emit_error(error: PipelineError) -> int:
    sys.stderr.write(render_error(error))
    return error.exit_code


def _install_signal_handlers(signal_flag: Dict[str, Optional[str]]) -> None:
    def handler(signum, _frame):
        # Only recorded here; the optimizer observes it at iteration boundaries.
        signal_flag["requested"] = signal.Signals(signum).name

    for signum in SIGNAL_EXIT_CODES:
        signal.signal(signum, handler)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="diff-render",
        description="Local differentiable microfacet rendering fit pipeline",
    )
    parser.add_argument("config", help="path to the JSON scene description")
    parser.add_argument(
        "--serial",
        action="store_true",
        help="force serial tile execution (determinism reference path)",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="ignore existing checkpoints and start from iteration 0",
    )
    parser.add_argument(
        "--strict-digest",
        action="store_true",
        help="return E_DIGEST instead of a fresh start when no state matches",
    )
    args = parser.parse_args(argv)

    # Validate before creating any output artifact.
    try:
        config = load_config(args.config)
    except PipelineError as error:
        return _emit_error(error)

    base_dir = os.path.dirname(os.path.abspath(args.config))
    paths = resolve_output_paths(config, base_dir)

    signal_flag: Dict[str, Optional[str]] = {"requested": None}
    _install_signal_handlers(signal_flag)

    try:
        report = run_pipeline(
            config,
            paths,
            parallel=not args.serial,
            force_restart=args.restart,
            strict_digest=args.strict_digest,
            signal_flag=signal_flag,
        )
    except PipelineError as error:
        return _emit_error(error)

    sys.stdout.write(json.dumps(report, sort_keys=True, ensure_ascii=False) + "\n")
    if report.get("interrupted"):
        signum = getattr(signal, report.get("interrupt_signal", "SIGTERM"), signal.SIGTERM)
        return SIGNAL_EXIT_CODES.get(signum, 143)
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
