#!/usr/bin/env python3
"""End-to-end library integration example.

Two fresh executions produce byte-identical stdout: each run starts a new
loopback receiver and uses a new temporary SQLite database.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tunnel_kernel import (
    ForwardEngine,
    LoopbackEchoService,
    Store,
    build_plan,
    load_config,
    query_echo,
    synthetic_payload,
)

def main() -> int:
    bundle = load_config(
        ROOT / "samples" / "peers.json",
        ROOT / "samples" / "links.json",
        ROOT / "samples" / "policy.json",
    )
    service = LoopbackEchoService(udp_port=0, tcp_port=0)
    ports = service.start()
    peer = replace(bundle.peers["udp-alpha"], port=ports["udp_port"])
    output = {"runs": []}
    try:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.sqlite3")
            store.initialize()
            for payload_size, seed, scenario_name in (
                (384, 1, None),
                (1024, 7, "degrade-udp"),
            ):
                payload = synthetic_payload(peer.name, payload_size, seed)
                plan = build_plan(
                    peer=peer,
                    payload=payload,
                    policy=bundle.policy,
                    link=bundle.links[peer.name],
                )
                store.prepare_tunnel(
                    peer=peer,
                    payload=payload,
                    policy=bundle.policy,
                    link=bundle.links[peer.name],
                    algorithm=plan.decision.algorithm,
                    mtu=plan.mtu,
                    message_id=plan.message_id,
                    fragments=plan.fragments,
                    clock_start_ms=bundle.policy.clock_start_ms,
                )
                scenario = (
                    bundle.scenarios[scenario_name].changes
                    if scenario_name is not None
                    else ()
                )
                result = ForwardEngine(
                    store=store,
                    peer=peer,
                    link=bundle.links[peer.name],
                    policy=bundle.policy,
                    scenario_changes=scenario,
                ).forward(plan.tunnel_id)
                echo_summary = query_echo(
                    peer,
                    bundle.policy,
                    message_id=plan.message_id,
                )
                output["runs"].append(
                    {
                        "algorithm": plan.decision.as_dict(),
                        "echo": echo_summary,
                        "forward": result,
                        "verify": store.verify_local(plan.tunnel_id),
                    }
                )
    finally:
        service.stop()
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
