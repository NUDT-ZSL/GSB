"""Command line entry point.

Usage (identical on Windows and POSIX):
    python -m chart_reader generate --out samples --seed 20261120 --count 6
    python -m chart_reader run --samples samples --config config/config.example.json \
        --faults config/faults.example.json --out reports/report.json \
        --snapshots snapshots
    python -m chart_reader verify --report reports/report.json

All files are read/written as UTF-8. No network access is ever performed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import codes
from .config import load_config
from .errors import BusinessError
from .generator import generate_samples
from .injection import InjectionPlan
from .pipeline import report_hash, run_pipeline, write_report
from .snapshots import SnapshotStore


def _cmd_generate(args: argparse.Namespace) -> int:
    records = generate_samples(args.out, args.seed, args.count,
                               args.width, args.height)
    print(f"generated {len(records)} charts into {args.out} "
          f"(seed={args.seed})")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
    except BusinessError as exc:
        print(json.dumps(exc.to_record(), ensure_ascii=False))
        return 2
    plan = InjectionPlan.load(args.faults)
    store = SnapshotStore(args.snapshots) if args.snapshots else None
    report = run_pipeline(args.samples, cfg, plan, store)
    write_report(report, args.out)
    n_codes = len(report["business_codes"])
    print(f"report written to {args.out}")
    print(f"report_sha256={report['report_sha256']}")
    print(f"self_check={report['self_check']['status']} "
          f"business_codes={n_codes} "
          f"injections={len(report['injections'])} "
          f"unattributed={len(report['unattributed_anomalies'])}")
    return 1 if report["self_check"]["status"] != "ok" else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    digest = report_hash(args.report)
    data = json.loads(Path(args.report).read_text(encoding="utf-8"))
    stored = data.get("report_sha256")
    ok = digest == stored
    print(f"recomputed={digest}")
    print(f"stored     ={stored}")
    print("match" if ok else "MISMATCH")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chart_reader",
        description="Deterministic local chart-image numeric reading engine")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate deterministic samples")
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=20261120)
    g.add_argument("--count", type=int, default=6)
    g.add_argument("--width", type=int, default=640)
    g.add_argument("--height", type=int, default=480)
    g.set_defaults(func=_cmd_generate)

    r = sub.add_parser("run", help="run the full reading pipeline")
    r.add_argument("--samples", required=True)
    r.add_argument("--config", required=True)
    r.add_argument("--faults", default=None)
    r.add_argument("--out", required=True)
    r.add_argument("--snapshots", default=None)
    r.set_defaults(func=_cmd_run)

    v = sub.add_parser("verify", help="recompute and compare report hash")
    v.add_argument("--report", required=True)
    v.set_defaults(func=_cmd_verify)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BusinessError as exc:
        print(json.dumps(exc.to_record(), ensure_ascii=False),
              file=sys.stderr)
        return 2 if exc.code != codes.E_CONFIG_INVALID else 3


if __name__ == "__main__":
    sys.exit(main())
