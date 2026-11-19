from __future__ import annotations

import argparse
import json
from pathlib import Path

from elastic_control.engine import Controller
from elastic_control.launcher import HTTPControlClient
from elastic_control.models import load_input_bundle
from elastic_control.policy import parse_policy
from elastic_control.store import Store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--services", required=True)
    parser.add_argument("--nodes", required=True)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--round-id", required=True)
    parser.add_argument("--tick", required=True, type=int)
    parser.add_argument("--service", required=True)
    parser.add_argument("--budget", type=int, default=8)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    bundle = load_input_bundle(args.services, args.nodes, args.trace)
    store = Store(args.db, args.runtime_dir)
    store.initialize()
    controller = Controller(
        bundle=bundle,
        policy=parse_policy(
            {
                "logical_time_budget_ticks": args.budget,
                "action_timeout_seconds": 1.0,
            }
        ),
        store=store,
        launcher=HTTPControlClient(args.url),
    )
    result = controller.run_round(
        round_id=args.round_id,
        tick=args.tick,
        service_filter=[args.service],
        budget_ticks=args.budget,
    )
    Path(args.out).write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

