"""Command line interface for the calibration workbench.

Usage:
  python -m calib_workbench.cli init  [--config configs/example_config.json]
  python -m calib_workbench.cli run   [--config configs/example_config.json]
  python -m calib_workbench.cli report [--config ...]
  python -m calib_workbench.cli selfcheck [--config ...]
  python -m calib_workbench.cli replay [--config ...]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys

from .config import DEFAULTS, load_config
from .data_gen import generate_manifest, load_manifest, write_manifest
from .errors import WorkbenchError
from .pipeline import run_pipeline
from .report import render_report
from .selfcheck import replay_lineage, run_selfcheck


def _cmd_init(args):
    cfg = copy.deepcopy(DEFAULTS)
    os.makedirs(os.path.dirname(args.config) or ".", exist_ok=True)
    with open(args.config, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
    rows = generate_manifest(cfg)
    os.makedirs(os.path.dirname(cfg["data"]["path"]) or ".", exist_ok=True)
    write_manifest(rows, cfg["data"]["path"])
    print(f"config  -> {args.config}")
    print(f"manifest-> {cfg['data']['path']} ({len(rows)} rows)")


def _cmd_run(args):
    cfg = load_config(args.config)
    report = run_pipeline(cfg)
    path = render_report(report, cfg["output_dir"])
    totals = report["plan"]["budget"]
    print(f"status          : {report['status']}")
    print(f"change          : {report['change']['kind']} ({report['change']['details']})")
    print(f"budget iters    : {totals['est_iters']} / {totals['max_total_iters']}")
    print(f"budget mem peak : {totals['peak_resident_bytes']} / {totals['max_resident_bytes']}")
    for kind, ptr in report["snapshots"].items():
        print(f"snapshot {kind:<11}: v{ptr['version']} changed={ptr['changed']}")
    for cal, m in sorted(report["overall"].items()):
        print(f"overall {cal:<10}: ECE={m['ece']:.6f} Brier={m['brier']:.6f} n={m['n']}")
    if report["errors"]:
        print(f"worker errors   : {len(report['errors'])} (see report)")
    print(f"report          : {path}")


def _cmd_report(args):
    cfg = load_config(args.config)
    with open(os.path.join(cfg["output_dir"], "report_data.json"), "r", encoding="utf-8") as fh:
        report = json.load(fh)
    print(render_report(report, cfg["output_dir"]))


def _load_report(cfg):
    with open(os.path.join(cfg["output_dir"], "report_data.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _cmd_selfcheck(args):
    cfg = load_config(args.config)
    rows = load_manifest(cfg["data"]["path"])
    result = run_selfcheck(cfg, rows, _load_report(cfg))
    for check in result["checks"]:
        print(f"[ok] {check['check']}")
    print("selfcheck: OK")


def _cmd_replay(args):
    cfg = load_config(args.config)
    result = replay_lineage(cfg, _load_report(cfg))
    for rec in result["replayed"]:
        print(f"[ok] {rec['combo']:<24} ECE={rec['ece']:.6f} Brier={rec['brier']:.6f}")
    print(f"replay: OK ({len(result['replayed'])} lineage records)")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="calib_workbench")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("init", _cmd_init), ("run", _cmd_run), ("report", _cmd_report),
                     ("selfcheck", _cmd_selfcheck), ("replay", _cmd_replay)):
        p = sub.add_parser(name)
        p.add_argument("--config", default="configs/example_config.json")
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except WorkbenchError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
