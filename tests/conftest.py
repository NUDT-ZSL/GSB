from __future__ import annotations

import copy
import json
import socket
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tunnel_kernel.config import (  # noqa: E402
    ConfigBundle,
    LinkProfile,
    Peer,
    load_config,
    synthetic_payload,
)
from tunnel_kernel.echo_service import LoopbackEchoService  # noqa: E402
from tunnel_kernel.planner import TunnelPlan, build_plan  # noqa: E402
from tunnel_kernel.store import Store  # noqa: E402


@pytest.fixture(scope="session")
def bundle() -> ConfigBundle:
    return load_config(
        ROOT / "samples" / "peers.json",
        ROOT / "samples" / "links.json",
        ROOT / "samples" / "policy.json",
    )


@pytest.fixture()
def echo_service() -> LoopbackEchoService:
    service = LoopbackEchoService(udp_port=0, tcp_port=0)
    service.start()
    try:
        yield service
    finally:
        service.stop()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def peer_for_ports(peer: Peer, ports: dict[str, int]) -> Peer:
    port = ports["udp_port"] if peer.protocol == "udp" else ports["tcp_port"]
    return replace(peer, port=port)


def prepare_tunnel(
    *,
    store: Store,
    peer: Peer,
    link: LinkProfile,
    policy: Any,
    payload_size: int,
    seed: int = 0,
) -> tuple[TunnelPlan, dict[str, Any]]:
    payload = synthetic_payload(peer.name, payload_size, seed)
    plan = build_plan(peer=peer, payload=payload, policy=policy, link=link)
    state = store.prepare_tunnel(
        peer=peer,
        payload=payload,
        policy=policy,
        link=link,
        algorithm=plan.decision.algorithm,
        mtu=plan.mtu,
        message_id=plan.message_id,
        fragments=plan.fragments,
        clock_start_ms=policy.clock_start_ms,
    )
    return plan, state


def write_cli_configs(
    tmp_path: Path,
    *,
    ports: dict[str, int],
    policy_updates: dict[str, Any] | None = None,
) -> dict[str, Path]:
    peers = json.loads((ROOT / "samples" / "peers.json").read_text(encoding="utf-8"))
    for peer in peers["peers"]:
        peer["listen"]["port"] = (
            ports["udp_port"] if peer["protocol"] == "udp" else ports["tcp_port"]
        )
    links = json.loads((ROOT / "samples" / "links.json").read_text(encoding="utf-8"))
    policy = json.loads((ROOT / "samples" / "policy.json").read_text(encoding="utf-8"))
    if policy_updates:
        policy.update(copy.deepcopy(policy_updates))
    paths = {
        "peers": tmp_path / "peers.json",
        "links": tmp_path / "links.json",
        "policy": tmp_path / "policy.json",
    }
    paths["peers"].write_text(json.dumps(peers), encoding="utf-8")
    paths["links"].write_text(json.dumps(links), encoding="utf-8")
    paths["policy"].write_text(json.dumps(policy), encoding="utf-8")
    return paths


def cli_config_args(paths: dict[str, Path], db: Path) -> list[str]:
    return [
        "--inventory",
        str(paths["peers"]),
        "--links",
        str(paths["links"]),
        "--policy",
        str(paths["policy"]),
        "--db",
        str(db),
    ]
