"""Deterministic in-repository sample generation."""

from __future__ import annotations

import random
from typing import Any

from .model import PROJECT_FORMAT, PROJECT_VERSION


DEFAULT_SEED = 20261128


def generate_project(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    ponds = ["P01", "P02", "P03"]
    sensor_specs = [
        ("DO", "dissolved_oxygen", 30, 3200, 200, 450, 760),
        ("PH", "ph", 25, 2400, 260, 320, 560),
        ("TEMP", "water_temperature", 20, 2000, 320, 260, 430),
    ]
    streams: list[dict[str, Any]] = []

    for pond in ponds:
        for suffix, sensor_type, priority, window, cadence, low_size, high_size in sensor_specs:
            frames: list[dict[str, Any]] = []
            time_ms = 0 if suffix == "DO" else (65 if suffix == "PH" else 125)
            sequence = 1
            while time_ms <= 2050:
                frames.append(
                    {
                        "frame_id": f"{pond}-{suffix}-{sequence:03d}",
                        "logical_time_ms": time_ms,
                        "length_bytes": rng.randint(low_size, high_size),
                        "ready": True,
                    }
                )
                sequence += 1
                time_ms += cadence
            streams.append(
                {
                    "stream_id": f"{pond}/{suffix}",
                    "pond_id": pond,
                    "kind": "sensor",
                    "sensor_type": sensor_type,
                    "priority": priority,
                    "window_bytes": window,
                    "frames": frames,
                }
            )

        control_frames: list[dict[str, Any]] = []
        for sequence, time_ms in enumerate([100, 520, 950, 1480, 1900], start=1):
            control_frames.append(
                {
                    "frame_id": f"{pond}-AERATOR-{sequence:03d}",
                    "logical_time_ms": time_ms + ponds.index(pond) * 45,
                    "length_bytes": rng.randint(180, 260),
                    "ready": True,
                }
            )
        streams.append(
            {
                "stream_id": f"{pond}/AERATOR-01",
                "pond_id": pond,
                "kind": "control",
                "target_device": f"{pond.lower()}-aerator-01",
                "priority": 100,
                "window_bytes": 1600,
                "deadline_ms": 220,
                "frames": control_frames,
            }
        )

    history_times = [100, 520, 950, 1480, 1880]
    history_sizes = [14000, 16000, 12000, 15000, 11000]
    streams.append(
        {
            "stream_id": "P02/HISTORY",
            "pond_id": "P02",
            "kind": "history",
            "sensor_type": "dissolved_oxygen_history",
            "priority": 120,
            "window_bytes": 30000,
            "frames": [
                {
                    "frame_id": f"P02-HISTORY-{index + 1:03d}",
                    "logical_time_ms": time_ms,
                    "length_bytes": size + (seed % 97),
                    "ready": True,
                }
                for index, (time_ms, size) in enumerate(zip(history_times, history_sizes))
            ],
        }
    )
    streams.sort(key=lambda item: item["stream_id"])

    return {
        "format": PROJECT_FORMAT,
        "project_version": PROJECT_VERSION,
        "name": "三塘口水产边缘网关固定种子工程",
        "seed": seed,
        "network": {
            "uplink_bytes_per_s": 90000,
            "rtt_ms": 140,
            "max_concurrent_streams": 1,
            "frame_size_limit_bytes": 24000,
        },
        "consumer": {
            "read_bytes_per_s": 25000,
            "events": [
                {"time_ms": 500, "read_bytes_per_s": 5000},
                {"time_ms": 1500, "read_bytes_per_s": 30000},
            ],
        },
        "outages": [{"start_ms": 1000, "end_ms": 1250}],
        "streams": streams,
        "metadata": {
            "scenario": "history_upload_blocks_aeration_control",
            "deterministic": True,
            "generated_by": "aqua_edge.generator.generate_project",
        },
    }


def generate_boundary_project() -> dict[str, Any]:
    return {
        "format": PROJECT_FORMAT,
        "project_version": PROJECT_VERSION,
        "name": "手工可核对边界样本",
        "seed": DEFAULT_SEED,
        "network": {
            "uplink_bytes_per_s": 1000,
            "rtt_ms": 100,
            "max_concurrent_streams": 1,
            "frame_size_limit_bytes": 1000,
        },
        "consumer": {
            "read_bytes_per_s": 1000000,
            "events": [],
        },
        "outages": [{"start_ms": 50, "end_ms": 150}],
        "streams": [
            {
                "stream_id": "PX/AERATOR-01",
                "pond_id": "PX",
                "kind": "control",
                "target_device": "px-aerator-01",
                "priority": 10,
                "window_bytes": 500,
                "deadline_ms": 100,
                "frames": [
                    {
                        "frame_id": "PX-CTRL-001",
                        "logical_time_ms": 0,
                        "length_bytes": 50,
                        "ready": True,
                    }
                ],
            },
            {
                "stream_id": "PX/HISTORY",
                "pond_id": "PX",
                "kind": "history",
                "sensor_type": "history_curve",
                "priority": 5,
                "window_bytes": 500,
                "frames": [
                    {
                        "frame_id": "PX-HIST-001",
                        "logical_time_ms": 0,
                        "length_bytes": 100,
                        "ready": True,
                    },
                    {
                        "frame_id": "PX-HIST-002",
                        "logical_time_ms": 200,
                        "length_bytes": 100,
                        "ready": True,
                    },
                ],
            },
        ],
        "metadata": {
            "scenario": "single_slot_with_rtt_and_outage",
            "deterministic": True,
        },
    }


def generate_invalid_examples() -> dict[str, dict[str, Any]]:
    base = generate_boundary_project()
    order_bad = _clone(base)
    order_bad["streams"][1]["frames"][0]["logical_time_ms"] = 300

    duplicate_stream = _clone(base)
    duplicate_stream["streams"][1]["stream_id"] = duplicate_stream["streams"][0]["stream_id"]

    frame_length = _clone(base)
    frame_length["streams"][0]["frames"][0]["length_bytes"] = 0

    return {
        "invalid_order": order_bad,
        "invalid_duplicate_stream": duplicate_stream,
        "invalid_frame_length": frame_length,
    }


def _clone(value: dict[str, Any]) -> dict[str, Any]:
    import copy

    return copy.deepcopy(value)
