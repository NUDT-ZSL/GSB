"""Command line entry: generate / analyze / verify."""

import argparse
import json
import os
import sys

from . import __version__
from .aggregate import aggregate
from .attribute import attribute_events
from .errors import BatchError, emit_error
from .events import detect_events
from .generator import generate
from .model import load_catalog, load_strategy
from .probes import load_probes
from .report import (build_report, canonical_json, write_aggregates_atomic,
                     write_json_atomic)
from .summary import build_team_summary

CATALOG_FILE = "catalog.json"
STRATEGY_FILE = "strategy.json"
PROBES_FILE = "probes.jsonl"
GROUND_TRUTH_FILE = "ground_truth.json"
REPORT_FILE = "report.json"
AGGREGATES_FILE = "aggregates.jsonl"


def cmd_generate(args):
    catalog, strategy, records, ground_truth = generate(args.seed)
    os.makedirs(args.dir, exist_ok=True)
    write_json_atomic(os.path.join(args.dir, CATALOG_FILE), catalog)
    write_json_atomic(os.path.join(args.dir, STRATEGY_FILE), strategy)
    tmp = os.path.join(args.dir, PROBES_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    os.replace(tmp, os.path.join(args.dir, PROBES_FILE))
    write_json_atomic(os.path.join(args.dir, GROUND_TRUTH_FILE), ground_truth)
    print(f"generated {len(records)} probe records, "
          f"{len(ground_truth['faults'])} injected faults -> {args.dir}")
    return 0


def run_analysis(directory):
    catalog = load_catalog(os.path.join(directory, CATALOG_FILE))
    strategy = load_strategy(os.path.join(directory, STRATEGY_FILE))
    records = load_probes(os.path.join(directory, PROBES_FILE), catalog)
    aggregates = aggregate(records, strategy["window_length"])
    events, sporadic = detect_events(aggregates, records, strategy)
    events = attribute_events(events, catalog, strategy)
    team_summary = build_team_summary(events, catalog, strategy)
    report = build_report(
        strategy, events, sporadic, team_summary,
        {"catalog": os.path.join(directory, CATALOG_FILE),
         "strategy": os.path.join(directory, STRATEGY_FILE),
         "probes": os.path.join(directory, PROBES_FILE)},
    )
    write_aggregates_atomic(os.path.join(directory, AGGREGATES_FILE), aggregates)
    write_json_atomic(os.path.join(directory, REPORT_FILE), report)
    return report


def cmd_analyze(args):
    report = run_analysis(args.dir)
    print(f"analysis complete: {report['event_count']} events, "
          f"parameter_version={report['parameter_version']} -> {args.dir}")
    return 0


def cmd_verify(args):
    from .verify import run_verification
    return run_verification(args.seed, args.dir)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="probeattr",
        description="Synthetic probe generation and availability attribution")
    parser.add_argument("--version", action="version",
                        version=f"probeattr {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="generate synthetic probes and ground truth")
    p_gen.add_argument("--seed", type=int, required=True)
    p_gen.add_argument("--dir", required=True)
    p_gen.set_defaults(func=cmd_generate)

    p_ana = sub.add_parser("analyze", help="aggregate, detect events and attribute")
    p_ana.add_argument("--dir", required=True)
    p_ana.set_defaults(func=cmd_analyze)

    p_ver = sub.add_parser("verify", help="run end-to-end acceptance checks")
    p_ver.add_argument("--seed", type=int, required=True)
    p_ver.add_argument("--dir", required=True)
    p_ver.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BatchError as err:
        emit_error(err)
        return 2


if __name__ == "__main__":
    sys.exit(main())
