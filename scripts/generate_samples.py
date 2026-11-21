"""Generate the committed deterministic demo scenario.

Run from the repository root:
    python scripts/generate_samples.py
"""

from __future__ import annotations

import json
from pathlib import Path

TARGETS = [
    ("healthy", "/healthy", 200, 500, 100, 3),
    ("lossy", "/lossy", 200, 500, 100, 3),
    ("slow", "/slow", 200, 500, 250, 3),
    ("jitter", "/jitter", 200, 500, 250, 3),
    ("http_status", "/status503", 200, 500, 100, 3),
    ("malformed", "/malformed", 200, 500, 100, 3),
    ("budgeted", "/budget", 200, 500, 400, 3),
    ("extra_a", "/healthy", 200, 500, 100, 3),
    ("extra_b", "/healthy", 200, 500, 100, 3),
    ("extra_c", "/healthy", 200, 500, 100, 3),
]

CLOCK_STEPS = [index * 500 for index in range(8)]


def build_scenario() -> dict:
    targets = [
        {
            "name": name,
            "endpoint": f"http://127.0.0.1:18080{path}",
            "expected_status": expected,
            "period_ms": period,
            "timeout_ms": timeout,
            "failure_threshold": threshold,
        }
        for name, path, expected, period, timeout, threshold in TARGETS
    ]
    rounds = []
    sequence = 1
    for round_index, tick in enumerate(CLOCK_STEPS, start=1):
        probes = []
        for target in TARGETS:
            probes.append(
                {
                    "seq": sequence,
                    "target": target[0],
                    "type": "http",
                }
            )
            sequence += 1
        rounds.append(
            {
                "round": round_index,
                "logical_tick": tick,
                "probes": probes,
            }
        )
    return {
        "schema_version": 1,
        "scenario_id": "loopback-fault-matrix-v1",
        "targets": targets,
        "probe_script": {
            "clock_step_table": CLOCK_STEPS,
            "rounds": rounds,
        },
        "config": {
            "max_concurrency": 3,
            "round_budget_ms": 300,
            "window_size": 10,
            "window_step": 5,
            "loss_rate_threshold": 0.3,
            "latency_jitter_threshold_ms": 10.0,
            "tolerance": {
                "loss_rate": 0.001,
                "latency_ms": 0.5,
            },
            "clock_step_ms": 5,
        },
    }


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "samples" / "scenario_demo.json"
    payload = build_scenario()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(path)


if __name__ == "__main__":
    main()
