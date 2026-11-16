#!/usr/bin/env python3
"""CLI for the deterministic image-to-text interop diagnostic workbench.

Usage:
  python3 run.py gen-samples --data data
  python3 run.py run --data data --out out [--serial]
  python3 run.py verify --data data --out out_verify
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from workbench.pipeline import run_pipeline
from workbench.samples import generate_samples


def _cmd_gen_samples(args) -> int:
    written = generate_samples(args.data)
    print(f"generated {len(written)} sample files under {args.data}/")
    for name in sorted(written):
        print(f"  {name} ({written[name]} bytes)")
    return 0


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum, _frame):
        print(f"\nreceived signal {signum}; finishing current region and "
              "persisting progress before exit", file=sys.stderr)
        stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported platform


def _cmd_run(args) -> int:
    stop = threading.Event()
    _install_signal_handlers(stop)
    code, result = run_pipeline(args.data, args.out, serial=args.serial,
                                stop_event=stop)
    report = result["report"]
    print(json.dumps({
        "exit_code": code,
        "snapshot": report.get("snapshot"),
        "regions": report.get("regions"),
        "render_diff": report.get("render_diff"),
        "round_trip": report.get("round_trip"),
        "injections_hit": [i["id"] for i in report.get("injections", [])
                           if i.get("hit")],
        "boundary_sensitive": [b["inst_id"]
                               for b in report.get("boundary_sensitive", [])],
        "failures": len(report.get("failures", [])),
    }, indent=2, ensure_ascii=False))
    print(f"report: {Path(args.out) / 'report.json'}")
    return code


def _cmd_verify(args) -> int:
    out = Path(args.out)
    code_s, serial_res = run_pipeline(args.data, out / "serial", serial=True)
    code_p, par_res = run_pipeline(args.data, out / "parallel", serial=False)
    a, b = serial_res["canonical"], par_res["canonical"]
    keys = ["regions", "classified", "gen_v1_text", "gen_v2_text",
            "downgrade_loss", "failures", "boundary_sensitive",
            "render_diff", "round_trip"]
    mismatches = [k for k in keys if a.get(k) != b.get(k)]
    ok = not mismatches and code_s == 0 and code_p == 0
    print(json.dumps({
        "serial_exit": code_s, "parallel_exit": code_p,
        "compared_keys": keys, "mismatches": mismatches,
        "round_trip": a.get("round_trip"),
        "consistent": ok,
    }, indent=2, ensure_ascii=False))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("gen-samples")
    p.add_argument("--data", default="data")
    p.set_defaults(fn=_cmd_gen_samples)
    p = sub.add_parser("run")
    p.add_argument("--data", default="data")
    p.add_argument("--out", default="out")
    p.add_argument("--serial", action="store_true")
    p.set_defaults(fn=_cmd_run)
    p = sub.add_parser("verify")
    p.add_argument("--data", default="data")
    p.add_argument("--out", default="out_verify")
    p.set_defaults(fn=_cmd_verify)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
