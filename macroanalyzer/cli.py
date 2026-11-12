"""Command line interface.

Exit codes:
  0  success
  2  partial failure (a document is parse_error / timeout / size_exceeded),
     or verify found mismatches
  3  ruleset failed to load (analysis continued with the builtin ruleset)
  4  argument error (no report file is written)
"""

import argparse
import os
import sys

from .analyzer import (MAX_WORKERS_LIMIT, PARTIAL_FAILURE_STATUSES,
                       analyze_documents)
from .generator import generate_samples
from .report import (build_report, merge_reports, verify_report, write_report)
from .rules import load_ruleset

EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_RULES = 3
EXIT_ARGS = 4


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print("error: %s" % message, file=sys.stderr)
        raise SystemExit(EXIT_ARGS)


def _positive_int(value):
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not an integer" % value)
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("%r must be a positive integer"
                                         % value)
    return ivalue


def build_parser():
    parser = _Parser(prog="macroanalyzer",
                     description="Offline Office macro payload static analyzer")
    sub = parser.add_subparsers(dest="command", required=True)

    pa = sub.add_parser("analyze", help="analyze one or more documents")
    pa.add_argument("inputs", nargs="+", help="document paths")
    pa.add_argument("--out", required=True, help="report JSON output path")
    pa.add_argument("--rules", default=None, help="ruleset JSON path "
                    "(default: builtin ruleset)")
    pa.add_argument("--jobs", type=_positive_int, default=4,
                    help="worker threads (capped at %d)" % MAX_WORKERS_LIMIT)
    pa.add_argument("--max-bytes", type=_positive_int,
                    default=16 * 1024 * 1024,
                    help="per-document size limit in bytes")
    pa.add_argument("--timeout-ms", type=_positive_int, default=5000,
                    help="per-document deep-parse timeout in milliseconds")

    pm = sub.add_parser("merge", help="merge legacy reports into the "
                        "current schema")
    pm.add_argument("inputs", nargs="+", help="old report JSON paths")
    pm.add_argument("--out", required=True, help="merged report output path")

    pv = sub.add_parser("verify", help="verify report integrity")
    pv.add_argument("--report", required=True, help="report JSON path")
    pv.add_argument("--rules", default=None,
                    help="ruleset JSON path (default: path stored in report)")

    pg = sub.add_parser("gensamples", help="generate deterministic samples")
    pg.add_argument("--out", required=True, help="output directory")
    return parser


def _cmd_analyze(args):
    for path in args.inputs:
        if not os.path.isfile(path):
            print("error: input file not found: %s" % path, file=sys.stderr)
            return EXIT_ARGS
    ruleset, rules_error = load_ruleset(args.rules)
    records, peak, workers = analyze_documents(
        args.inputs, ruleset, args.max_bytes, args.timeout_ms, args.jobs)
    report = build_report(
        records, ruleset, args.rules, rules_error,
        concurrency_peak=peak, max_workers=workers,
        max_bytes=args.max_bytes, timeout_ms=args.timeout_ms)
    write_report(report, args.out)
    if rules_error:
        print("rules_load_error: %s" % rules_error, file=sys.stderr)
        return EXIT_RULES
    if any(r["status"] in PARTIAL_FAILURE_STATUSES for r in records):
        return EXIT_PARTIAL
    return EXIT_OK


def _cmd_merge(args):
    for path in args.inputs:
        if not os.path.isfile(path):
            print("error: report file not found: %s" % path, file=sys.stderr)
            return EXIT_ARGS
    report, warnings = merge_reports(args.inputs)
    write_report(report, args.out)
    for warning in warnings:
        print("warning: %s" % warning, file=sys.stderr)
    return EXIT_OK


def _cmd_verify(args):
    if not os.path.isfile(args.report):
        print("error: report file not found: %s" % args.report,
              file=sys.stderr)
        return EXIT_ARGS
    ok, mismatches = verify_report(args.report, args.rules)
    if ok:
        print("OK")
        return EXIT_OK
    for mismatch in mismatches:
        print("MISMATCH: %s" % mismatch)
    return EXIT_PARTIAL


def _cmd_gensamples(args):
    written = generate_samples(args.out)
    for path in written:
        print(path)
    return EXIT_OK


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "merge":
        return _cmd_merge(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "gensamples":
        return _cmd_gensamples(args)
    return EXIT_ARGS
