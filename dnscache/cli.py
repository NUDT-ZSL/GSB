"""Command line interface: load / resolve / warmup / diagnose / replace /
resume / status / verify."""
import argparse
import json
import os
import sys

from .errors import BizError
from .resolver import UpstreamClient
from .service import (
    op_load, op_replace, op_resolve, op_warmup, op_resume, op_diagnose,
    op_status, op_verify,
)
from .store import Store

DEFAULT_TOLERANCE_MS = 1000


def _emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _store(args):
    return Store(args.state)


def _upstream(args):
    if getattr(args, "no_upstream", False):
        return None
    return UpstreamClient(args.upstream_host, args.upstream_port)


def _clock_us(store, args):
    if getattr(args, "clock_us", None) is not None:
        return int(args.clock_us)
    return store.get_clock_us()


def _add_common(parser):
    parser.add_argument("--state", default="./state",
                        help="state directory (SQLite ledger + lock)")
    parser.add_argument("--tolerance-ms", type=int,
                        default=DEFAULT_TOLERANCE_MS)


def _add_upstream(parser):
    parser.add_argument("--upstream-host", default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=15353)
    parser.add_argument("--no-upstream", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(prog="dnscache")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load", help="validate + merge + commit snapshots")
    _add_common(p)
    p.add_argument("--v1", help="v1 snapshot JSON path")
    p.add_argument("--v2", help="v2 snapshot directory")
    p.add_argument("--clock-us", type=int, required=True)

    p = sub.add_parser("resolve", help="resolve one name/type")
    _add_common(p)
    _add_upstream(p)
    p.add_argument("--name", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--clock-us", type=int)

    p = sub.add_parser("warmup", help="run query templates with clock steps")
    _add_common(p)
    _add_upstream(p)
    p.add_argument("--queries", required=True)
    p.add_argument("--clock-table", required=True)

    p = sub.add_parser("diagnose", help="precision findings as stable JSON")
    _add_common(p)

    p = sub.add_parser("replace", help="apply a v2 snapshot package")
    _add_common(p)
    p.add_argument("--v2", required=True)
    p.add_argument("--clock-us", type=int, required=True)

    p = sub.add_parser("resume", help="replay interrupted operations")
    _add_common(p)
    _add_upstream(p)

    p = sub.add_parser("status", help="show store status")
    _add_common(p)

    p = sub.add_parser("verify", help="consistency checks")
    _add_common(p)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    store = _store(args)
    try:
        if args.command == "load":
            _emit(op_load(store, args.clock_us, args.tolerance_ms,
                          v1_path=args.v1, v2_dir=args.v2))
        elif args.command == "resolve":
            _emit(op_resolve(store, args.name, args.type,
                             _clock_us(store, args), args.tolerance_ms,
                             _upstream(args)))
        elif args.command == "warmup":
            with open(args.queries, "r", encoding="utf-8") as fh:
                queries = json.load(fh)["queries"]
            with open(args.clock_table, "r", encoding="utf-8") as fh:
                steps = json.load(fh)["steps"]
            crash = os.environ.get("DNSCACHE_CRASH_AFTER_STEP")
            _emit(op_warmup(store, queries, steps, args.tolerance_ms,
                            _upstream(args),
                            int(crash) if crash is not None else None))
        elif args.command == "diagnose":
            _emit(op_diagnose(store))
        elif args.command == "replace":
            _emit(op_replace(store, args.v2, args.clock_us,
                             args.tolerance_ms))
        elif args.command == "resume":
            _emit(op_resume(store, args.tolerance_ms, _upstream(args)))
        elif args.command == "status":
            _emit(op_status(store))
        elif args.command == "verify":
            report = op_verify(store)
            _emit(report)
            return 0 if report["ok"] else 1
    except BizError as exc:
        json.dump({"error": exc.to_dict()}, sys.stderr, indent=2,
                  sort_keys=True)
        sys.stderr.write("\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
