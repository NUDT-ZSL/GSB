"""Command line interface: load / merge / query / resume / status / verify.

All output is stable-sorted, fixed-indent UTF-8 JSON so repeated runs are
byte-for-byte comparable. The logical clock is injected via --clock-ms;
the wall clock is never consulted."""

import argparse
import json
import os
import shutil
import subprocess
import sys

from .config import Config
from .engine import (
    cmd_load,
    cmd_merge,
    cmd_query,
    cmd_resume,
    cmd_status,
    load_manifest,
)
from .errors import BusinessError
from .stablejson import dumps


def _emit(obj):
    sys.stdout.write(dumps(obj))


def _run_step(fn, *args, **kwargs):
    """Run one engine step; business errors become structured output."""
    try:
        return fn(*args, **kwargs)
    except BusinessError as exc:
        return {"status": "error", **exc.to_dict()}


def _derive_config(config, state_dir):
    """Clone the config with a private state DB / lock file."""
    with open(config.path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    raw["base_dir"] = config.base_dir
    raw["state_db"] = os.path.join(state_dir, "state.sqlite")
    raw["lock_file"] = os.path.join(state_dir, "engine.lock")
    derived_path = os.path.join(state_dir, "config.json")
    with open(derived_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, sort_keys=True, indent=2)
    return Config(derived_path)


def cmd_verify(config, clock_ms):
    """Deterministic end-to-end suite against the fixed samples.

    Runs in a private state directory so repeated invocations produce
    byte-identical output. Exercises merge adjudication, the restricted
    query subset, fragment checksum failure, clock regression, forced
    termination and recovery."""
    manifest = load_manifest(config)
    clocks = manifest["clocks"]
    verify_dir = os.path.join(os.path.dirname(config.state_db),
                              "verify_state")
    if os.path.isdir(verify_dir):
        shutil.rmtree(verify_dir)
    os.makedirs(verify_dir)
    vconfig = _derive_config(config, verify_dir)
    report = {"status": "ok", "steps": {}}
    report["steps"]["load"] = _run_step(cmd_load, vconfig, clocks["load"])
    report["steps"]["merge"] = _run_step(cmd_merge, vconfig, clocks["merge"])
    report["steps"]["merge_idempotent"] = _run_step(
        cmd_merge, vconfig, clocks["merge"]
    )
    report["steps"]["query_legal"] = _run_step(
        cmd_query, vconfig, clocks["query"], "legal.json"
    )
    report["steps"]["query_legal_replay"] = _run_step(
        cmd_query, vconfig, clocks["query"], "legal.json", batch_no=1
    )
    report["steps"]["query_invalid_syntax"] = _run_step(
        cmd_query, vconfig, clocks["query"] + 1, "invalid_syntax.json"
    )
    report["steps"]["query_type_mismatch"] = _run_step(
        cmd_query, vconfig, clocks["query"] + 2, "type_mismatch.json"
    )
    report["steps"]["clock_regression"] = _run_step(
        cmd_query, vconfig, clocks["regressed"], "legal.json"
    )
    report["steps"]["status_before_crash"] = _run_step(cmd_status, vconfig)
    crash_merge = subprocess.run(
        [
            sys.executable, "-m", "docengine.cli",
            "--config", vconfig.path,
            "merge",
            "--clock-ms", str(clocks["crash_merge"]),
            "--crash-after", "staging",
        ],
        capture_output=True,
        text=True,
    )
    report["steps"]["crash_merge_exit_code"] = crash_merge.returncode
    crash_query = subprocess.run(
        [
            sys.executable, "-m", "docengine.cli",
            "--config", vconfig.path,
            "query",
            "--clock-ms", str(clocks["crash_query"]),
            "--templates", "legal.json",
            "--crash-after", "running",
        ],
        capture_output=True,
        text=True,
    )
    report["steps"]["crash_query_exit_code"] = crash_query.returncode
    report["steps"]["resume"] = _run_step(cmd_resume, vconfig,
                                          clocks["resume"])
    report["steps"]["status_after_resume"] = _run_step(cmd_status, vconfig)
    report["steps"]["query_after_resume"] = _run_step(
        cmd_query, vconfig, clocks["post_resume"], "legal.json"
    )
    shutil.rmtree(verify_dir)
    return report


def build_parser():
    parser = argparse.ArgumentParser(prog="docengine")
    parser.add_argument("--config", required=True, help="path to config JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_clock(p, default):
        p.add_argument("--clock-ms", type=int, default=default,
                       help="injected logical clock in milliseconds")

    p = sub.add_parser("load", help="validate both snapshots")
    add_clock(p, 3000)

    p = sub.add_parser("merge", help="merge snapshots and commit a generation")
    add_clock(p, 3100)
    p.add_argument("--crash-after", choices=["staging"], default=None,
                   help="test hook: hard-kill after the named phase")

    p = sub.add_parser("query", help="execute a query template batch")
    add_clock(p, 3200)
    p.add_argument("--templates", default="legal.json",
                   help="template file inside the templates directory")
    p.add_argument("--batch-no", type=int, default=None,
                   help="explicit batch number (idempotent replay)")
    p.add_argument("--crash-after", choices=["running"], default=None,
                   help="test hook: hard-kill after the named phase")

    p = sub.add_parser("resume", help="recover after a forced termination")
    add_clock(p, 3300)

    sub.add_parser("status", help="print version pointer and watermarks")

    p = sub.add_parser("verify", help="run the deterministic end-to-end suite")
    add_clock(p, 3000)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = Config(args.config)
    try:
        if args.command == "load":
            out = cmd_load(config, args.clock_ms)
        elif args.command == "merge":
            out = cmd_merge(config, args.clock_ms,
                            crash_after=args.crash_after)
        elif args.command == "query":
            out = cmd_query(config, args.clock_ms, args.templates,
                            batch_no=args.batch_no,
                            crash_after=args.crash_after)
        elif args.command == "resume":
            out = cmd_resume(config, args.clock_ms)
        elif args.command == "status":
            out = cmd_status(config)
        elif args.command == "verify":
            out = cmd_verify(config, args.clock_ms)
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except BusinessError as exc:
        out = {"status": "error", **exc.to_dict()}
    _emit(out)
    return 0 if out.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
