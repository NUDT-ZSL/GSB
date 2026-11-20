"""Generate the fixed deterministic sample files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


SEED = 20261121
MASK64 = (1 << 64) - 1
ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
    return (value ^ (value >> 31)) & MASK64


class DeterministicRandom:
    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        return splitmix64(self.state)

    def between(self, low: int, high: int) -> int:
        return low + self.next() % (high - low + 1)


def write_json(path: Path, value: object) -> bytes:
    data = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    path.write_bytes(data)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_config() -> dict[str, object]:
    return {
        "schema_version": 1,
        "classes": [
            {
                "name": "access",
                "parent": None,
                "cir_bps": 12_000_001,
                "pir_bps": 16_000_003,
                "committed_burst_bytes": 24_000,
                "peak_burst_bytes": 48_000,
                "queue_depth_bytes": 64_000,
                "drop_policy": "tail",
                "weight": 1,
                "guarantee_bps": None,
            },
            {
                "name": "interactive",
                "parent": "access",
                "cir_bps": 3_500_000,
                "pir_bps": 5_500_007,
                "committed_burst_bytes": 8_000,
                "peak_burst_bytes": 16_000,
                "queue_depth_bytes": 14_000,
                "drop_policy": "tail",
                "weight": 3,
                "guarantee_bps": 3_000_000,
            },
            {
                "name": "bulk",
                "parent": "access",
                "cir_bps": 6_000_000,
                "pir_bps": 8_000_009,
                "committed_burst_bytes": 12_000,
                "peak_burst_bytes": 24_000,
                "queue_depth_bytes": 18_000,
                "drop_policy": "wred",
                "weight": 2,
                "guarantee_bps": 5_000_000,
                "wred": {
                    "min_bytes": 6_000,
                    "max_bytes": 16_000,
                    "max_probability_ppm": 350_000,
                },
            },
            {
                "name": "telemetry",
                "parent": "access",
                "cir_bps": 2_500_001,
                "pir_bps": 4_000_003,
                "committed_burst_bytes": 5_000,
                "peak_burst_bytes": 10_000,
                "queue_depth_bytes": 9_000,
                "drop_policy": "tail",
                "weight": 1,
                "guarantee_bps": 2_000_000,
            },
        ],
        "classifiers": [
            {
                "id": "tls-in",
                "type": "five_tuple",
                "target": "interactive",
                "five_tuple": {
                    "src_ip": "198.51.100.10",
                    "dst_ip": "203.0.113.20",
                    "src_port": 51000,
                    "dst_port": 443,
                    "protocol": "tcp",
                },
            },
            {
                "id": "voice-dscp",
                "type": "dscp",
                "target": "interactive",
                "dscp": 46,
            },
            {
                "id": "telemetry-dscp",
                "type": "dscp",
                "target": "telemetry",
                "dscp": 10,
            },
            {
                "id": "bulk-dscp",
                "type": "dscp",
                "target": "bulk",
                "dscp": 8,
            },
        ],
    }


def build_events() -> dict[str, object]:
    rng = DeterministicRandom(SEED)
    events: list[dict[str, object]] = []
    seq = 0

    def add_event(step: int, class_name: str) -> None:
        nonlocal seq
        seq += 1
        arrival = step * 5 + rng.between(0, 4)
        byte_count = rng.between(240, 1_840)
        event: dict[str, object] = {
            "seq": seq,
            "bytes": byte_count,
            "arrival_ms": arrival,
            "class": class_name,
        }
        if class_name == "interactive":
            if seq % 2:
                event["dscp"] = 46
            else:
                event["dscp"] = 46
                event["five_tuple"] = {
                    "src_ip": "198.51.100.10",
                    "dst_ip": "203.0.113.20",
                    "src_port": 51_000 + (seq % 1_000),
                    "dst_port": 443,
                    "protocol": "tcp",
                }
        elif class_name == "bulk":
            event["dscp"] = 8
        elif class_name == "telemetry":
            event["dscp"] = 10
        events.append(event)

    for step in range(100):
        if step % 6 == 0:
            add_event(step, "interactive")
        if step % 4 == 0:
            add_event(step, "bulk")
        if step % 9 == 0:
            add_event(step, "telemetry")
        if 18 <= step <= 21:
            for _ in range(14):
                add_event(step, "bulk")
        if 42 <= step <= 44:
            for _ in range(18):
                add_event(step, "interactive")
        if 66 <= step <= 68:
            for _ in range(24):
                add_event(step, "telemetry")
    return {"schema_version": 1, "events": events}


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    config_path = SAMPLES / "config.json"
    arrivals_path = SAMPLES / "arrivals.json"
    config_bytes = write_json(config_path, build_config())
    arrivals_bytes = write_json(arrivals_path, build_events())
    manifest = {
        "schema_version": 1,
        "sample_id": "qos-fixed-hierarchical-001",
        "injection_id": "inject-20261121-deterministic",
        "seed": SEED,
        "step_ms": 5,
        "total_steps": 120,
        "window_ms": 250,
        "throughput_tolerance_bps": 100_000,
        "schedule_tolerance_bytes": 800,
        "initial_token_fill": "full",
        "files": {
            "config": "config.json",
            "arrivals": "arrivals.json",
        },
        "sha256": {
            "config": sha256(config_bytes),
            "arrivals": sha256(arrivals_bytes),
        },
    }
    write_json(SAMPLES / "manifest.json", manifest)
    print(
        json.dumps(
            {
                "config_sha256": manifest["sha256"]["config"],
                "arrivals_sha256": manifest["sha256"]["arrivals"],
                "events": len(build_events()["events"]),
                "sample_id": manifest["sample_id"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

