"""Command line interface equivalent to the FastAPI endpoints.

Usage:
  python -m coord_workbench.cli gen-samples SAMPLES_DIR
  python -m coord_workbench.cli merge      --samples DIR --workdir DIR [--max-concurrency N --budget MS]
  python -m coord_workbench.cli recover    --samples DIR --workdir DIR
  python -m coord_workbench.cli verify     --samples DIR
  python -m coord_workbench.cli state      --samples DIR --workdir DIR
  python -m coord_workbench.cli topology   --samples DIR
  python -m coord_workbench.cli serve      --samples DIR --workdir DIR [--port N]
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import Samples, build_state_report, form_batches, run_scheduler, verify_replay_consistency
from .errors import BizError
from .persistence import Ledger
from .recovery import recover
from .seedgen import generate_all


def _emit(obj: object) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False))


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="coord_workbench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gen-samples")
    p.add_argument("out")
    p.add_argument("--seed", type=int, default=None)

    def add_common(sp: argparse.ArgumentParser, need_workdir: bool = True) -> None:
        sp.add_argument("--samples", required=True)
        if need_workdir:
            sp.add_argument("--workdir", required=True)

    p = sub.add_parser("merge")
    add_common(p)
    p.add_argument("--max-concurrency", type=int, default=16)
    p.add_argument("--budget", type=int, default=100_000)
    p.add_argument("--crash-after-batch", type=int, default=None)

    p = sub.add_parser("recover")
    add_common(p)

    p = sub.add_parser("verify")
    add_common(p, need_workdir=False)

    p = sub.add_parser("state")
    add_common(p)

    p = sub.add_parser("topology")
    add_common(p, need_workdir=False)

    p = sub.add_parser("serve")
    add_common(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "gen-samples":
            from .seedgen import DEFAULT_SEED

            _emit(generate_all(args.out, seed=args.seed or DEFAULT_SEED))
            return 0
        if args.cmd == "serve":
            import os

            import uvicorn

            os.environ["CWB_WORKDIR"] = args.workdir
            os.environ["CWB_SAMPLE_DIR"] = args.samples
            uvicorn.run("coord_workbench.api:app", host=args.host, port=args.port)
            return 0

        samples = Samples(args.samples)
        if args.cmd == "verify":
            _emit(verify_replay_consistency(samples))
        elif args.cmd == "topology":
            _emit(samples.topo_index)
        elif args.cmd == "merge":
            ledger = Ledger(args.workdir)
            former, batches = form_batches(samples)
            records = run_scheduler(
                ledger,
                batches,
                samples.nodes_by_name,
                args.max_concurrency,
                args.budget,
                crash_after_batch=args.crash_after_batch,
            )
            _emit({"schedule": records, "state": build_state_report(ledger, samples, former)})
        elif args.cmd == "recover":
            ledger = Ledger(args.workdir)
            _, batches = form_batches(samples)
            _emit({"recovery": recover(ledger, batches, samples.nodes_by_name),
                   "state": build_state_report(ledger, samples)})
        elif args.cmd == "state":
            ledger = Ledger(args.workdir)
            _emit(build_state_report(ledger, samples))
        return 0
    except BizError as exc:
        _emit({"error": exc.to_dict()})
        return 2


if __name__ == "__main__":
    sys.exit(main())
