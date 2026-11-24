"""Deterministic fixed-seed sample generator."""
from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Optional

from .checkpoint import atomic_write_json
from .fixedpoint import FIXED_SCALE, FIXED_SCALE_VERSION, SAMPLE_SCHEMA_VERSION
from .fragmentation import plan_all
from .parser import parse_sample

FIXED_SEED = 20261125
LOGICAL_CLOCK_ORIGIN_MS = 0
WINDOWS_DEFAULT_WORKSPACE = r"C:\vpn_diag_lab"
WINDOWS_SAMPLE_PATH = r"C:\vpn_diag_lab\samples\fixed_seed_sample.json"
RELATIVE_SAMPLE_PATH = Path("samples") / "fixed_seed_sample.json"

FIXED_PORTS = {
    "atlas": 39101,
    "beacon": 39102,
    "shift": 39103,
    "stall": 39104,
    "precision": 39105,
    "infeasible": 39106,
}


def _addr(name: str) -> str:
    return f"127.0.0.1:{FIXED_PORTS[name]}"


def build_sample() -> dict[str, Any]:
    rng = random.Random(FIXED_SEED)
    atlas_lengths = [rng.randint(180, 900) for _ in range(3)]
    beacon_lengths = [300, 700]

    sample: dict[str, Any] = {
        "schema_version": SAMPLE_SCHEMA_VERSION,
        "fixed_scale": FIXED_SCALE,
        "fixed_point_version": FIXED_SCALE_VERSION,
        "seed": FIXED_SEED,
        "logical_clock_origin_ms": LOGICAL_CLOCK_ORIGIN_MS,
        "windows_default_workspace": WINDOWS_DEFAULT_WORKSPACE,
        "windows_sample_path": WINDOWS_SAMPLE_PATH,
        "profiles": [
            {
                "name": "fiber",
                "rtt_ms": 8,
                "loss_rate": 0.01,
                "bandwidth_kbps": 5000,
                "jitter_limit": 0.15,
            },
            {
                "name": "dsl",
                "rtt_ms": 10,
                "loss_rate": 0.04,
                "bandwidth_kbps": 2000,
                "jitter_limit": 0.10,
            },
            {
                "name": "mobile",
                "rtt_ms": 12,
                "loss_rate": 0.05,
                "bandwidth_kbps": 1200,
                "jitter_limit": 0.20,
            },
            {
                "name": "mobile-degraded",
                "rtt_ms": 30,
                "loss_rate": 0.10,
                "bandwidth_kbps": 400,
                "jitter_limit": 0.25,
            },
            {
                "name": "lossy",
                "rtt_ms": 15,
                "loss_rate": 0.20,
                "bandwidth_kbps": 500,
                "jitter_limit": 0.10,
            },
            {
                "name": "precision-probe",
                "rtt_ms": 23.333,
                "loss_rate": 0,
                "bandwidth_kbps": 3072,
                "jitter_limit": 0,
            },
            {
                "name": "bad-loss-rate",
                "rtt_ms": 4,
                "loss_rate": 1.25,
                "bandwidth_kbps": 100,
                "jitter_limit": 0.2,
            },
            {
                "name": "bad-bandwidth",
                "rtt_ms": 4,
                "loss_rate": 0.1,
                "bandwidth_kbps": -1,
                "jitter_limit": 0.2,
            },
        ],
        "policy": {
            "fragment_threshold": 512,
            "concurrency_per_peer": 2,
            "round_budget_ms": 24.666,
            "retransmit_budget": 2,
            "tolerance_ms": 0.001,
            "clock_step_ms": 5,
            "max_rounds": 24,
            "udp_timeout_ms": 200,
            "checkpoint_every": 1,
        },
        "peers": [
            {
                "name": "atlas",
                "echo_addr": _addr("atlas"),
                "outer_mtu": 1400,
                "crypto_overhead": 28,
                "link_weight": 1,
                "inner_payload_estimate_max": 1024,
                "length_distribution": {
                    "kind": "fixed_seed_random_lengths",
                    "seed": FIXED_SEED,
                    "lengths": atlas_lengths,
                },
                "payload_lengths": atlas_lengths,
                "profile": "fiber",
            },
            {
                "name": "beacon",
                "echo_addr": _addr("beacon"),
                "outer_mtu": 1200,
                "crypto_overhead": 24,
                "link_weight": 1,
                "inner_payload_estimate_max": 768,
                "length_distribution": {"lengths": beacon_lengths},
                "payload_lengths": beacon_lengths,
                "profile": "dsl",
            },
            {
                "name": "shift",
                "echo_addr": _addr("shift"),
                "outer_mtu": 1400,
                "crypto_overhead": 28,
                "link_weight": 1,
                "inner_payload_estimate_max": 256,
                "length_distribution": {"lengths": [250]},
                "payload_lengths": [250],
                "profile": "mobile",
            },
            {
                "name": "stall",
                "echo_addr": _addr("stall"),
                "outer_mtu": 900,
                "crypto_overhead": 20,
                "link_weight": 1,
                "inner_payload_estimate_max": 220,
                "length_distribution": {"lengths": [200]},
                "payload_lengths": [200],
                "profile": "lossy",
            },
            {
                "name": "precision",
                "echo_addr": _addr("precision"),
                "outer_mtu": 512,
                "crypto_overhead": 0,
                "link_weight": 1,
                "inner_payload_estimate_max": 512,
                "length_distribution": {"lengths": [512]},
                "payload_lengths": [512],
                "profile": "precision-probe",
            },
            {
                "name": "infeasible",
                "echo_addr": _addr("infeasible"),
                "outer_mtu": 64,
                "crypto_overhead": 100,
                "link_weight": 1,
                "inner_payload_estimate_max": 1,
                "length_distribution": {"lengths": [1]},
                "payload_lengths": [1],
                "profile": "fiber",
            },
            {
                "name": "",
                "echo_addr": "127.0.0.1:39201",
                "outer_mtu": 1200,
                "crypto_overhead": 20,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [100],
                "profile": "fiber",
            },
            {
                "name": "duplicate-address",
                "echo_addr": _addr("atlas"),
                "outer_mtu": 1200,
                "crypto_overhead": 20,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [100],
                "profile": "fiber",
            },
            {
                "name": "tiny-mtu",
                "echo_addr": "127.0.0.1:39203",
                "outer_mtu": 32,
                "crypto_overhead": 10,
                "inner_payload_estimate_max": 128,
                "payload_lengths": [128],
                "profile": "fiber",
            },
            {
                "name": "negative-weight",
                "echo_addr": "127.0.0.1:39204",
                "outer_mtu": 1200,
                "crypto_overhead": 20,
                "link_weight": -2,
                "inner_payload_estimate_max": 100,
                "payload_lengths": [100],
                "profile": "fiber",
            },
        ],
        "events": [
            {
                "type": "loss_segment",
                "peer": "shift",
                "at_clock_ms": 0,
                "until_clock_ms": 5,
                "description": "drop the first logical acknowledgement for profile-switch closure",
            },
            {
                "type": "profile_change",
                "peer": "shift",
                "at_clock_ms": 5,
                "profile_name": "mobile-degraded",
            },
            {
                "type": "retransmit",
                "peer": "shift",
                "at_clock_ms": 50,
                "description": "scheduled recovery probe aligned with recalculated RTO",
            },
            {
                "type": "loss_segment",
                "peer": "stall",
                "at_clock_ms": 0,
                "until_clock_ms": 100000,
                "description": "persistent deterministic loss segment",
            },
            {
                "type": "mystery_event",
                "peer": "atlas",
                "at_clock_ms": 1,
            },
        ],
    }

    parsed = parse_sample(sample)
    plans, _errors, _markers = plan_all(parsed.peers, parsed.policy)
    for raw_peer in sample["peers"]:
        name = raw_peer.get("name")
        if isinstance(name, str) and name in plans:
            raw_peer["sync_markers"] = [spec.marker for spec in plans[name]]
    return sample


def default_output_path() -> Path:
    if Path(WINDOWS_DEFAULT_WORKSPACE).drive or sys_platform_is_windows():
        return Path(WINDOWS_SAMPLE_PATH)
    return Path.cwd() / RELATIVE_SAMPLE_PATH


def sys_platform_is_windows() -> bool:
    import sys

    return sys.platform.startswith("win")


def write_sample(output: Optional[str | Path] = None) -> Path:
    destination = Path(output) if output else default_output_path()
    sample = build_sample()
    atomic_write_json(destination, sample)
    return destination


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the fixed-seed VPN diagnostic sample")
    parser.add_argument("--output", "-o", default=None, help="output JSON path")
    args = parser.parse_args(argv)
    path = write_sample(args.output)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
