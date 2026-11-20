"""Command line interface.

  python -m gateway.cli validate --samples samples
  python -m gateway.cli run --samples samples --db artifacts/gateway.db \
      --out artifacts/runs/run.json [--force-mode MODE] [--crash-after N]
  python -m gateway.cli evaluate --samples samples \
      --workdir artifacts/eval --out artifacts/runs/eval.json
  python -m gateway.cli serve --port 8377

Exit codes: 0 success, 2 business/validation error (E_xxx on stderr),
3 simulated hard kill (--crash-after), 1 unexpected error.
"""
import argparse
import json
import sys

from . import models
from .engine import Engine
from .errors import GatewayError
from .evaluate import evaluate
from .loader import load_samples
from .store import Store
from .upstream_server import main as serve_main


def _dump(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


def _cmd_validate(args):
    services, nodes, trace = load_samples(args.samples)
    print(_dump({
        "status": "ok",
        "services": sorted(services),
        "nodes": sorted(nodes),
        "windows": len(trace.windows),
    }), end="")
    return 0


def _cmd_run(args):
    # Validation completes before any persistent state is created.
    services, nodes, trace = load_samples(args.samples)
    store = Store(args.db)
    try:
        engine = Engine(services, nodes, trace, store,
                        force_mode=args.force_mode,
                        crash_after_requests=args.crash_after)
        report = engine.run()
    finally:
        store.close()
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_dump(report))
    print(_dump({"status": "ok", "out": args.out,
                "rounds": report["summary"]["rounds_committed"]}), end="")
    return 0


def _cmd_evaluate(args):
    services, nodes, trace = load_samples(args.samples)
    report = evaluate(services, nodes, trace, args.workdir)
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_dump(report))
    print(_dump({"status": "ok", "out": args.out}), end="")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate")
    p.add_argument("--samples", required=True)
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("run")
    p.add_argument("--samples", required=True)
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--force-mode",
                   choices=(models.MODE_PER_REQUEST, models.MODE_WINDOW_BATCH),
                   default=None)
    p.add_argument("--crash-after", type=int, default=None)
    p.set_defaults(func=_cmd_run)

    p = sub.add_parser("evaluate")
    p.add_argument("--samples", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=_cmd_evaluate)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8377)
    p.set_defaults(func=lambda a: serve_main(["--port", str(a.port)]))

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GatewayError as exc:
        sys.stderr.write(_dump(exc.to_dict()))
        return 2


if __name__ == "__main__":
    sys.exit(main())
