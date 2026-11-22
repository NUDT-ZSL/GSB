#!/usr/bin/env python3
"""Generate deterministic local samples used by tests and manual verification."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def queue(
    index: int,
    *,
    weight: int,
    max_irq_rate: int,
    rx_usecs: int,
    rx_frames: int,
    tx_usecs: int,
    tx_frames: int,
) -> dict[str, Any]:
    return {
        "coalesce": {
            "rx_frames": rx_frames,
            "rx_usecs": rx_usecs,
            "tx_frames": tx_frames,
            "tx_usecs": tx_usecs,
        },
        "max_irq_rate": max_irq_rate,
        "queue": index,
        "weight": weight,
    }


def build_sample() -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for index in range(30):
        events.append(
            {
                "arrival_frames": 2,
                "direction": "rx",
                "frame_bytes": 1000,
                "iface": "eth0",
                "logical_clock_us": (index + 1) * 10,
                "queue": 0,
            }
        )
    q1_frames = [1, 2, 2, 1, 2, 2, 1, 2, 2, 1, 2, 2, 1, 2, 2, 1, 2, 2, 1, 2]
    for index, frames in enumerate(q1_frames):
        events.append(
            {
                "arrival_frames": frames,
                "direction": "rx",
                "frame_bytes": 1200,
                "iface": "eth0",
                "logical_clock_us": (index + 1) * 15,
                "queue": 1,
            }
        )
    for index in range(25):
        events.append(
            {
                "arrival_frames": 1,
                "direction": "rx",
                "frame_bytes": 800,
                "iface": "eth1",
                "logical_clock_us": (index + 1) * 10,
                "queue": 0,
            }
        )
    events.sort(
        key=lambda item: (
            item["logical_clock_us"],
            item["iface"],
            item["queue"],
            item["frame_bytes"],
        )
    )
    traffic = []
    for offset, event in enumerate(events):
        item = copy.deepcopy(event)
        item["record_seq"] = 1000 + offset * 10
        traffic.append(item)
    return {
        "config": {
            "avg_frame_interval_us": "1.3333",
            "clock_step_us": 10,
            "error_bound_budget_us": "0.01",
            "fixed_scale": 1000,
            "float_tolerance": "1e-9",
            "irq_budget_per_second": {
                "eth0:0": 25000,
                "eth0:1": 25000,
                "eth1:0": 100000,
            },
        },
        "interfaces": [
            {
                "name": "eth0",
                "queues": [
                    queue(
                        0,
                        weight=4,
                        max_irq_rate=100000,
                        rx_usecs=30,
                        rx_frames=7,
                        tx_usecs=20,
                        tx_frames=3,
                    ),
                    queue(
                        1,
                        weight=2,
                        max_irq_rate=80000,
                        rx_usecs=0,
                        rx_frames=5,
                        tx_usecs=15,
                        tx_frames=2,
                    ),
                ],
            },
            {
                "name": "eth1",
                "queues": [
                    queue(
                        0,
                        weight=3,
                        max_irq_rate=120000,
                        rx_usecs=30,
                        rx_frames=3,
                        tx_usecs=10,
                        tx_frames=2,
                    )
                ],
            },
        ],
        "schema_version": 1,
        "traffic": traffic,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    sample = build_sample()
    write_json(SAMPLES / "sample.json", sample)

    duplicate = copy.deepcopy(sample)
    duplicate["interfaces"][0]["queues"].append(
        copy.deepcopy(duplicate["interfaces"][0]["queues"][0])
    )
    write_json(SAMPLES / "invalid_duplicate_queue.json", duplicate)

    weight = copy.deepcopy(sample)
    weight["interfaces"][0]["queues"][0]["weight"] = 0
    write_json(SAMPLES / "invalid_weight.json", weight)

    max_rate = copy.deepcopy(sample)
    max_rate["interfaces"][0]["queues"][0]["max_irq_rate"] = 0
    write_json(SAMPLES / "invalid_max_irq_rate.json", max_rate)

    rx_usecs = copy.deepcopy(sample)
    rx_usecs["interfaces"][0]["queues"][0]["coalesce"]["rx_usecs"] = -1
    write_json(SAMPLES / "invalid_rx_usecs.json", rx_usecs)

    rx_frames = copy.deepcopy(sample)
    rx_frames["interfaces"][0]["queues"][0]["coalesce"]["rx_frames"] = 1.5
    write_json(SAMPLES / "invalid_rx_frames.json", rx_frames)

    sequence = copy.deepcopy(sample)
    sequence["traffic"][1]["record_seq"] = sequence["traffic"][0]["record_seq"]
    write_json(SAMPLES / "invalid_record_seq.json", sequence)

    frames = copy.deepcopy(sample)
    frames["traffic"][0]["arrival_frames"] = -1
    write_json(SAMPLES / "invalid_arrival_frames.json", frames)

    byte_count = copy.deepcopy(sample)
    byte_count["traffic"][0]["frame_bytes"] = 0
    write_json(SAMPLES / "invalid_frame_bytes.json", byte_count)

    unknown_queue = copy.deepcopy(sample)
    unknown_queue["traffic"][0]["queue"] = 99
    write_json(SAMPLES / "invalid_unknown_queue.json", unknown_queue)

    weight_v2 = copy.deepcopy(sample)
    weight_v2["interfaces"][0]["queues"][0]["weight"] = 5
    write_json(SAMPLES / "params_v2_weight.json", weight_v2)

    coalesce_v2 = copy.deepcopy(sample)
    coalesce_v2["interfaces"][0]["queues"][0]["coalesce"]["rx_usecs"] = 35
    write_json(SAMPLES / "params_v2_coalesce.json", coalesce_v2)

    crash_plan = {
        "parameter_import": {
            "crash_point": "param_after_stage",
            "database": "artifacts/crash-param.sqlite",
            "initial_sample": "samples/sample.json",
            "replacement_sample": "samples/params_v2_coalesce.json",
            "replacement_version": 2,
        },
        "record_advance": {
            "crash_point": "record_before_commit",
            "database": "artifacts/crash-record.sqlite",
            "record_seq": 1000,
            "sample": "samples/sample.json",
        },
    }
    write_json(SAMPLES / "crash_plan.json", crash_plan)


if __name__ == "__main__":
    main()
