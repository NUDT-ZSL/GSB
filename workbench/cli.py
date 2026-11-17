"""Command line entry point.

Usage:
    python -m workbench.cli gen-samples --root .
    python -m workbench.cli run --config samples/config.json [--edits edits.json]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

from .errors import WorkbenchError
from .runner import Workbench


def _load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def cmd_run(args):
    config = _load_config(args.config)
    root = args.root or os.path.dirname(os.path.dirname(os.path.abspath(args.config)))
    wb = Workbench(config, root=root)

    def _handle_signal(signum, frame):
        # Graceful stop: finish the in-flight request, persist progress,
        # then exit 0. No exit report is promised after a hard kill.
        wb.request_stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        wb.load_inputs()
        if args.edits:
            with open(args.edits, "r", encoding="utf-8") as fh:
                for edit in json.load(fh):
                    wb.apply_edit(edit)
        result = wb.run()
    except WorkbenchError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    summary = {
        "requests": len(result["requests"]),
        "failures": len(result["failures"]),
        "cache": result["cache_stats"],
        "stopped": result["stopped"],
        "report": os.path.join(wb.workdir, "report.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_gen_samples(args):
    from scripts.gen_samples import generate

    generate(args.root)
    print(f"samples generated under {os.path.join(args.root, 'samples')}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the workbench")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--edits", default=None)
    p_run.add_argument("--root", default=None)
    p_run.set_defaults(func=cmd_run)

    p_gen = sub.add_parser("gen-samples", help="generate deterministic samples")
    p_gen.add_argument("--root", default=".")
    p_gen.set_defaults(func=cmd_gen_samples)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
