"""Deterministic local sample generation; no network or attachments are used."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .spec import block_data_filename, load_replica_group
from .util import atomic_write_bytes, atomic_write_json, digest_json


MARKER = ".generated-by-replica-ledger"
SHARD_SIZE = 512

NODE_PRIORITIES = {
    "n1": 10,
    "n2": 9,
    "n3": 7,
    "n4": 8,
    "n5": 6,
    "n6": 5,
}
NODE_REPAIR_LOADS = {
    "n1": 3,
    "n2": 0,
    "n3": 2,
    "n4": 8,
    "n5": 5,
    "n6": 7,
}

SCENARIOS: dict[str, dict[str, Any]] = {
    "single": {
        "group_id": "sample-single-mismatch",
        "nodes": ("n1", "n2", "n3", "n4"),
        "blocks": {"B-SINGLE": 1536},
        "mismatch": {"n4"},
        "checks": {"B-SINGLE": []},
    },
    "multi": {
        "group_id": "sample-multi-mismatch",
        "nodes": ("n1", "n2", "n3", "n4", "n5", "n6"),
        "blocks": {"B-MULTI": 2048},
        "mismatch": {"n5", "n6"},
        "checks": {"B-MULTI": []},
    },
    "grid": {
        "group_id": "sample-grid-perturbation",
        "nodes": ("n1", "n2", "n3", "n4", "n5", "n6"),
        "blocks": {"B-GRID": 768, "B-TIE": 1024},
        "mismatch": {
            "n6": {"B-GRID", "B-TIE"},
            "n4": {"B-TIE"},
            "n5": {"B-TIE"},
        },
        "checks": {"B-GRID": [], "B-TIE": ["B-GRID"]},
    },
}


def deterministic_bytes(seed: str, size: int) -> bytes:
    """Version-independent deterministic byte stream."""

    if size <= 0:
        raise ValueError("size must be positive")
    output = bytearray()
    counter = 0
    while len(output) < size:
        output.extend(
            hashlib.sha256(f"replica-ledger-sample|{seed}|{counter}".encode("utf-8")).digest()
        )
        counter += 1
    return bytes(output[:size])


def variant_bytes(seed: str, size: int) -> bytes:
    """Same-sized but content-divergent bytes for a mismatch replica."""

    data = bytearray(deterministic_bytes(seed, size))
    position = size // 2
    data[position] ^= 0x5A
    return bytes(data)


def _block_content(scenario: str, block_id: str, node_id: str) -> bytes:
    size = SCENARIOS[scenario]["blocks"][block_id]
    mismatch = SCENARIOS[scenario]["mismatch"]
    if isinstance(mismatch, set):
        is_variant = node_id in mismatch
    else:
        is_variant = node_id in mismatch and block_id in mismatch[node_id]
    if is_variant:
        # B-TIE intentionally creates two equal-sized 3-vs-3 content groups so
        # majority frequency alone cannot decide; all other mismatch blocks use
        # node-specific variants to exercise multiple distinct digests.
        variant_seed = (
            f"{scenario}:{block_id}:shared-variant"
            if block_id == "B-TIE"
            else f"{scenario}:{block_id}:{node_id}"
        )
        return variant_bytes(variant_seed, size)
    return deterministic_bytes(f"{scenario}:{block_id}:truth", size)


def _manifest(block_id: str, data: bytes, shard_size: int = SHARD_SIZE) -> dict[str, Any]:
    shards = []
    offset = 0
    index = 0
    while offset < len(data):
        chunk = data[offset : offset + shard_size]
        shards.append(
            {
                "index": index,
                "offset": offset,
                "byte_size": len(chunk),
                "digest": hashlib.sha256(chunk).hexdigest(),
            }
        )
        offset += len(chunk)
        index += 1
    return {"block_id": block_id, "byte_size": len(data), "shards": shards}


def scenario_spec(scenario: str) -> dict[str, Any]:
    config = SCENARIOS[scenario]
    nodes = []
    for node_id in config["nodes"]:
        block_rows = []
        for block_id in sorted(config["blocks"]):
            size = config["blocks"][block_id]
            data = _block_content(scenario, block_id, node_id)
            block_rows.append(_manifest(block_id, data))
        nodes.append(
            {
                "id": node_id,
                "directory": f"nodes/{node_id}",
                "priority": NODE_PRIORITIES[node_id],
                "repair_load": NODE_REPAIR_LOADS[node_id],
                "blocks": block_rows,
            }
        )
    return {
        "schema_version": 1,
        "group_id": config["group_id"],
        "digest_algorithm": "sha256",
        "digest_length": 64,
        "nodes": nodes,
        "checks": [
            {"block_id": block_id, "depends_on": list(dependencies)}
            for block_id, dependencies in sorted(config["checks"].items())
        ],
        "repair_policy": {
            "alert_ratio": {"numerator": 1, "denominator": 8},
            "cost": {
                "transfer_weight": 1,
                "work_weight": 1,
                "priority_weight": 1,
                "load_weight": 1,
                "transfer_overhead_numerator": 3,
                "transfer_overhead_denominator": 100,
                "work_numerator": 3,
                "work_denominator": 2,
            },
        },
    }


def _prepare_output(root: Path, *, force: bool) -> None:
    if root.exists() and any(root.iterdir()):
        marker = root / MARKER
        if not force and not marker.exists():
            raise FileExistsError(
                f"output directory is non-empty and not generated by this tool: {root}"
            )
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("replica-ledger deterministic samples\n", encoding="utf-8")


def generate_scenario(root: str | Path, scenario: str, *, force: bool = False) -> Path:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    output = Path(root)
    _prepare_output(output, force=force)
    spec = scenario_spec(scenario)
    for node in spec["nodes"]:
        node_dir = output / node["directory"]
        node_dir.mkdir(parents=True, exist_ok=True)
        for block in node["blocks"]:
            data = _block_content(scenario, block["block_id"], node["id"])
            atomic_write_bytes(
                node_dir / block_data_filename(block["block_id"]),
                data,
            )
    spec_path = output / "spec.json"
    atomic_write_json(spec_path, spec)
    # Validation is part of generation: a sample that does not load is a build
    # failure, not a runtime surprise.
    load_replica_group(spec_path)
    return spec_path


def generate_samples(root: str | Path, *, force: bool = False) -> dict[str, Any]:
    output = Path(root)
    _prepare_output(output, force=force)
    index: dict[str, Any] = {
        "generator": "replica-ledger deterministic samples",
        "scenario_order": ["single", "multi", "grid"],
        "scenarios": {},
    }
    for scenario in index["scenario_order"]:
        scenario_root = output / "scenarios" / scenario
        spec_path = generate_scenario(scenario_root, scenario, force=True)
        spec_bytes = spec_path.read_bytes()
        index["scenarios"][scenario] = {
            "spec_path": f"scenarios/{scenario}/spec.json",
            "spec_digest": digest_json(json.loads(spec_bytes.decode("utf-8"))),
            "scenario": scenario,
        }
    atomic_write_json(output / "sample_index.json", index)
    return index
