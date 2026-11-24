"""Interactive CLI: import / query / export / verify.

Exit codes: 0 = full success, 1 = partial failure (rejects, lock
conflict, checkpoint anomaly), 2 = argument/usage error.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import codes
from .checkpoint import atomic_write_json
from .engine import import_batch, verify_checkpoint
from .report import build_report, query_region

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


def _emit(payload):
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _cmd_import(args):
    if not args.declarations and not args.traces:
        _emit({"error": "nothing to import: pass --declarations and/or --traces"})
        return EXIT_USAGE
    outcome = import_batch(args.workdir, args.declarations or [], args.traces or [])
    _emit(outcome)
    if outcome["code"] in (codes.E_LOCK,) or outcome["rejects"] or outcome["anomalies"]:
        return EXIT_PARTIAL
    return EXIT_OK


def _cmd_query(args):
    _emit(query_region(args.workdir, args.region))
    return EXIT_OK


def _cmd_export(args):
    report = build_report(args.workdir)
    if args.out:
        atomic_write_json(args.out, report)
    _emit(report)
    has_violation = any(r["verdict"] == "VIOLATION" for r in report["regions"])
    return EXIT_PARTIAL if (has_violation or report["rejects"]) else EXIT_OK


def _cmd_verify(args):
    result = verify_checkpoint(args.workdir)
    _emit(result)
    return EXIT_OK if result["ok"] else EXIT_PARTIAL


def build_parser():
    parser = argparse.ArgumentParser(
        prog="memguard",
        description="Offline memory-boundary guard for deterministic access traces.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="import declarations and/or trace batches")
    p_import.add_argument("--workdir", required=True)
    p_import.add_argument("--declarations", nargs="*", default=[])
    p_import.add_argument("--traces", nargs="*", default=[])
    p_import.set_defaults(func=_cmd_import)

    p_query = sub.add_parser("query", help="query one region's boundary verdict")
    p_query.add_argument("--workdir", required=True)
    p_query.add_argument("--region", required=True)
    p_query.set_defaults(func=_cmd_query)

    p_export = sub.add_parser("export", help="export the diagnostic report (JSON)")
    p_export.add_argument("--workdir", required=True)
    p_export.add_argument("--out", default=None, help="atomic snapshot output path")
    p_export.set_defaults(func=_cmd_export)

    p_verify = sub.add_parser("verify", help="verify checkpoint chain consistency")
    p_verify.add_argument("--workdir", required=True)
    p_verify.set_defaults(func=_cmd_verify)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        _emit({"error": str(exc)})
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
