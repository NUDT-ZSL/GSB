#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path


SEED = 20261122
ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
BATCHES = SAMPLES / "batches"
SPILL = SAMPLES / "spill"
PROTOCOLS = ["TCP", "UDP", "QUIC"]
DIRECTIONS = ["ingress", "egress"]


def stable_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def main() -> int:
    BATCHES.mkdir(parents=True, exist_ok=True)
    SPILL.mkdir(parents=True, exist_ok=True)
    for old in BATCHES.glob("b*.jsonl"):
        old.unlink()

    rng = random.Random(SEED)
    endpoints = [
        *[f"lo:127.0.0.1:{port}" for port in range(41000, 41009)],
        *[f"lo:[::1]:{port}" for port in range(42000, 42009)],
    ]
    templates: list[dict[str, str]] = []
    seen_templates: set[tuple[str, str, str, str]] = set()
    while len(templates) < 160:
        src, dst = rng.sample(endpoints, 2)
        protocol = rng.choice(PROTOCOLS)
        direction = rng.choice(DIRECTIONS)
        key = (src, dst, protocol, direction)
        if key in seen_templates:
            continue
        seen_templates.add(key)
        templates.append(
            {
                "direction": direction,
                "dst_endpoint": dst,
                "protocol": protocol,
                "src_endpoint": src,
            }
        )

    plan = {
        "directions": DIRECTIONS,
        "dimensions": ["endpoint_pair", "protocol", "direction"],
        "fixed_scale": 1000,
        "key_normalization": {
            "direction_case": "lower",
            "endpoint_pair_order": "ordered",
            "key_separator": "|",
            "protocol_case": "upper",
        },
        "max_batch_rows": 64,
        "max_clock_regression_ms": 500,
        "memory_budget_mib": 0.03,
        "plan_id": "loopback-demo-v1",
        "protocols": ["TCP", "UDP", "QUIC"],
        "quantiles": [0.5, 0.9, 0.99],
        "read_buffer_fraction": 0.25,
        "spill_dir": "spill",
    }
    (SAMPLES / "plan.json").write_text(
        json.dumps(plan, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest_batches: list[dict[str, object]] = []
    clock_steps: list[dict[str, object]] = []
    record_seq = 0
    watermark = 0
    batch_count = 10
    rows_per_batch = 48
    for batch_number in range(1, batch_count + 1):
        batch_id = f"b{batch_number:03d}"
        base_clock = 1_000_000 + batch_number * 1_000
        rows = []
        batch_clocks: list[int] = []
        for row_number in range(rows_per_batch):
            record_seq += 1
            template = templates[(record_seq * 17 + batch_number * 5) % len(templates)]
            clock = base_clock + row_number * 7 + rng.randint(-3, 3)
            byte_value = rng.randint(64, 1500)
            packet_value = rng.randint(1, 20)
            row = {
                "bytes": byte_value,
                "clock_ms": clock,
                "direction": template["direction"],
                "dst_endpoint": template["dst_endpoint"],
                "packets": packet_value,
                "protocol": template["protocol"],
                "record_seq": record_seq,
                "src_endpoint": template["src_endpoint"],
            }
            rows.append(row)
            batch_clocks.append(clock)
        batch_path = BATCHES / f"{batch_id}.jsonl"
        payload = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
        batch_path.write_bytes(payload)
        sha256 = hashlib.sha256(payload).hexdigest()
        max_clock = max(batch_clocks)
        watermark = max(watermark, max_clock)
        manifest_batches.append(
            {
                "batch_id": batch_id,
                "path": f"batches/{batch_id}.jsonl",
                "sha256": sha256,
            }
        )
        clock_steps.append(
            {
                "batch_id": batch_id,
                "expected_watermark_ms": watermark,
                "max_clock_ms": max_clock,
                "min_clock_ms": min(batch_clocks),
                "record_count": len(rows),
                "sha256": sha256,
            }
        )

    (SAMPLES / "manifest.json").write_text(
        json.dumps(
            {"batches": manifest_batches, "seed": SEED},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (SAMPLES / "clock_steps.json").write_text(
        json.dumps(
            {"seed": SEED, "steps": clock_steps},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        stable_json(
            {
                "batches": batch_count,
                "bytes": sum(path.stat().st_size for path in BATCHES.glob("b*.jsonl")),
                "records": record_seq,
                "seed": SEED,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

