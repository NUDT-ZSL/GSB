from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .canon import canonical_text


DEFAULT_SEED = 417


def _plan() -> dict[str, Any]:
    return {
        "format": "gradient.training_plan/v1",
        "seed": DEFAULT_SEED,
        "logic_clock_ms": 0,
        "scheduling": {
            "max_concurrency": 2,
            "time_budget_ms": 5,
        },
        "sharding": {
            "method": "ring_contiguous",
            "ring_direction": "forward",
            "transport": "loopback",
        },
        "layers": [
            {
                "name": "embed_tokens",
                "parameter_bytes": 480,
                "shards": [
                    {"index": 0, "byte_size": 120},
                    {"index": 1, "byte_size": 160},
                    {"index": 2, "byte_size": 200},
                ],
            },
            {
                "name": "encoder_layer",
                "parameter_bytes": 640,
                "shards": [
                    {"index": 0, "byte_size": 100},
                    {"index": 1, "byte_size": 140},
                    {"index": 2, "byte_size": 180},
                    {"index": 3, "byte_size": 220},
                ],
            },
        ],
    }


def _nodes() -> dict[str, Any]:
    return {
        "format": "gradient.node_inventory/v1",
        "nodes": [
            {
                "node_id": "n1",
                "address": "127.0.0.1:5101",
                "compute_ratio": 1.0,
                "memory_limit_bytes": 4096,
            },
            {
                "node_id": "n2",
                "address": "127.0.0.1:5102",
                "compute_ratio": 0.8,
                "memory_limit_bytes": 4096,
            },
            {
                "node_id": "n3",
                "address": "127.0.0.1:5103",
                "compute_ratio": 0.6,
                "memory_limit_bytes": 700,
            },
            {
                "node_id": "n4",
                "address": "localhost:5104",
                "compute_ratio": 1.2,
                "memory_limit_bytes": 4096,
            },
        ],
    }


def _spec_v2() -> dict[str, Any]:
    return {
        "version": 2,
        "method": "ring_contiguous_dynamic",
        "sharding": {"method": "ring_contiguous_dynamic", "reshard_reason": "parameter_shape_update"},
        "layers": [
            {
                "name": "embed_tokens",
                "parameter_bytes": 480,
                "shards": [
                    {"index": 0, "byte_size": 260},
                    {"index": 1, "byte_size": 220},
                ],
            },
            {
                "name": "encoder_layer",
                "parameter_bytes": 640,
                "shards": [
                    {"index": 0, "byte_size": 200},
                    {"index": 1, "byte_size": 220},
                    {"index": 2, "byte_size": 220},
                ],
            },
        ],
    }


def _shard_waves(spec: dict[str, Any]) -> list[tuple[str, int]]:
    waves: list[tuple[str, int]] = []
    for layer in spec["layers"]:
        for shard in layer["shards"]:
            waves.append((layer["name"], shard["index"]))
    return waves


def _events(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    nodes = ["n1", "n2", "n3", "n4"]
    plan = _plan()
    v1_waves = _shard_waves(plan)
    v2_waves = _shard_waves(_spec_v2())
    records: list[dict[str, Any]] = []

    def add_wave(spec_version: int, wave_index: int, layer: str, shard: int, base_clock: int) -> None:
        for node_index, node_id in enumerate(nodes):
            clock = base_clock + node_index
            record = {
                "logic_clock_ms": clock,
                "node_id": node_id,
                "layer": layer,
                "shard_index": shard,
                "spec_version": spec_version,
                "expected_commit_ms": base_clock + 7,
            }
            records.append(record)
            if rng.random() < 0.18:
                duplicate = dict(record)
                duplicate["logic_clock_ms"] = clock + 1
                duplicate["duplicate"] = True
                records.append(duplicate)

    for wave_index, (layer, shard) in enumerate(v1_waves):
        add_wave(1, wave_index, layer, shard, wave_index * 8)

    v2_start = 84
    for wave_offset, (layer, shard) in enumerate(v2_waves):
        add_wave(2, wave_offset, layer, shard, v2_start + wave_offset * 8)

    events: list[dict[str, Any]] = []
    for seq, record in enumerate(records, start=1):
        event = {"event_seq": seq}
        event.update(record)
        events.append(event)
    return events


def _injections() -> dict[str, Any]:
    return {
        "format": "gradient.injections/v1",
        "injections": [
            {
                "seq": 1,
                "id": "inj-n2-fault",
                "type": "node_fault",
                "node_id": "n2",
                "logic_clock_ms": 25,
                "reason": "controlled_loopback_failure",
            },
            {
                "seq": 2,
                "id": "inj-n2-recover",
                "type": "node_recover",
                "node_id": "n2",
                "logic_clock_ms": 47,
                "reason": "loopback_restore",
            },
            {
                "seq": 3,
                "id": "inj-spec-v2",
                "type": "spec_replace",
                "logic_clock_ms": 72,
                "spec": _spec_v2(),
                "reason": "parameter_shard_adjustment",
            },
        ],
    }


def generate_samples(out_dir: str | Path = "samples", seed: int = DEFAULT_SEED) -> dict[str, Any]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    plan = _plan()
    nodes = _nodes()
    injections = _injections()
    events = _events(seed)

    (root / "training_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "nodes.json").write_text(
        json.dumps(nodes, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (root / "gradient_events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    (root / "injections.json").write_text(
        json.dumps(injections, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "ok": True,
        "out_dir": str(root),
        "seed": seed,
        "events": len(events),
        "nodes": len(nodes["nodes"]),
        "injections": len(injections["injections"]),
        "canonical_digest": __import__("gradient_engine.canon", fromlist=["sha256_json"]).sha256_json(
            {"plan": plan, "nodes": nodes, "events": events, "injections": injections}
        ),
    }
    (root / "generation_summary.json").write_text(
        canonical_text(summary) + "\n", encoding="utf-8"
    )
    return summary
