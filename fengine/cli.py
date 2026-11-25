"""Command line interface: load / plan / run / resume / status / verify / patch.

All diagnostics are emitted as stable JSON on stdout; business errors are
emitted as {"error": {code, message, details}} on stderr with exit code 2.
"""
import argparse
import json
import sys

from . import loader, runner, verify as verify_mod, versioning
from .errors import BizError
from .planner import build_plan
from .status import build_status
from .store import Store


def _emit(payload):
    print(json.dumps(payload, sort_keys=True, indent=2))


def cmd_load(args):
    graph, events, config = loader.load_artifacts(
        args.graph, args.events, args.config)
    store = Store(args.db)
    try:
        plan = build_plan(graph, events, config)
        store.load_artifacts(graph, events, config, plan["plan_summary"])
        _emit({"status": "loaded", "nodes": len(graph["nodes"]),
               "events": len(events), "windows": len(plan["windows"]),
               "plan_summary": plan["plan_summary"]})
    finally:
        store.close()


def cmd_plan(args):
    store = Store(args.db)
    try:
        graph = store.get_graph()
        events = store.get_events()
        config = store.get_config()
        plan = build_plan(graph, events, config)
        _emit({"plan_summary": plan["plan_summary"], "layers": plan["layers"],
               "windows": plan["windows"], "units": len(plan["units"])})
    finally:
        store.close()


def cmd_run(args, resume=False):
    store = Store(args.db)
    try:
        lo, hi = (-2**62, 2**62)
        if args.windows:
            lo, hi = (int(x) for x in args.windows.split(":"))
        stats = runner.run(
            store, worker_id=args.worker_id, slots=args.slots,
            window_lo=lo, window_hi=hi,
            crash_after=args.crash_after, resume=resume)
        _emit({"status": "ok", "resumed": resume, **stats,
               "logical_clock": store.get_clock()})
    finally:
        store.close()


def cmd_status(args):
    store = Store(args.db)
    try:
        _emit(build_status(store))
    finally:
        store.close()


def cmd_verify(args):
    store = Store(args.db)
    try:
        report = verify_mod.verify(store)
        _emit(report)
        if report["status"] != "ok":
            sys.exit(1)
    finally:
        store.close()


def cmd_patch(args):
    store = Store(args.db)
    try:
        new_graph = loader.validate_graph(loader.load_json_file(args.graph))
        report = versioning.apply_patch(store, new_graph,
                                        target_version=args.target_version)
        _emit(report)
    finally:
        store.close()


def cmd_serve(args):
    from .server import create_app
    app = create_app(args.db)
    app.run(host=args.host, port=args.port)


def build_parser():
    p = argparse.ArgumentParser(prog="fengine")
    p.add_argument("--db", default="fengine.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("load")
    sp.add_argument("--graph", required=True)
    sp.add_argument("--events", required=True)
    sp.add_argument("--config", required=True)
    sp.set_defaults(fn=cmd_load)

    sp = sub.add_parser("plan")
    sp.set_defaults(fn=cmd_plan)

    for name, is_resume in (("run", False), ("resume", True)):
        sp = sub.add_parser(name)
        sp.add_argument("--worker-id", default="w0")
        sp.add_argument("--slots", type=int, default=None)
        sp.add_argument("--windows", default=None,
                        help="window range LO:HI (hi exclusive)")
        sp.add_argument("--crash-after", type=int, default=None,
                        help="simulate hard kill after N committed units")
        sp.set_defaults(fn=(lambda a, r=is_resume: cmd_run(a, resume=r)))

    sp = sub.add_parser("status")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("verify")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("patch")
    sp.add_argument("--graph", required=True)
    sp.add_argument("--target-version", type=int, default=None)
    sp.set_defaults(fn=cmd_patch)

    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8071)
    sp.set_defaults(fn=cmd_serve)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except BizError as exc:
        print(json.dumps({"error": exc.to_dict()}, sort_keys=True),
              file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
