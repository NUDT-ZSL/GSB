"""Deterministic local sample generation.

All randomness derives from a single fixed-seed ``random.Random`` and a
``numpy`` default_rng built from that seed. The generator intentionally embeds:

* cold-start users (no interactions),
* long-tail items (only one interaction),
* periodic hot items (interaction rate follows a sine wave),
* one row for each parser business code so failure paths are exercised.

The largest scale is the base generation; smaller scales are deterministic
slices (first N users / first M items / first K interactions).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

from .util import stable_hash

BASE_USERS = 40
BASE_ITEMS = 60
BASE_INTERACTIONS = 900
COLD_USER_RATIO = 0.15
LONG_TAIL_TARGET_RATIO = 0.30
HOT_ITEM_COUNT = 5

VOCAB = [
    "python", "java", "rust", "go", "sql", "linux", "cloud", "docker",
    "network", "security", "graph", "vector", "cache", "stream", "batch",
    "index", "query", "model", "metric", "trace", "kernel", "memory",
]


def _rng(seed: int) -> tuple[Any, np.random.Generator]:
    import random

    py_rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)
    return py_rng, np_rng


def generate_base(seed: int, interaction_types: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    py_rng, np_rng = _rng(seed)
    items: list[dict[str, Any]] = []
    for item_idx in range(BASE_ITEMS):
        term_count = int(py_rng.randint(2, 4))
        terms = sorted(py_rng.sample(VOCAB, term_count))
        items.append({"item_id": f"item_{item_idx:04d}", "terms": terms})

    all_users = [f"user_{idx:04d}" for idx in range(BASE_USERS)]
    cold_count = max(1, round(BASE_USERS * COLD_USER_RATIO))
    cold_users = set(all_users[:cold_count])
    active_users = all_users[cold_count:]

    hot_ids = [f"item_{idx:04d}" for idx in range(HOT_ITEM_COUNT)]
    long_tail_count = max(1, round(BASE_ITEMS * LONG_TAIL_TARGET_RATIO))
    long_tail_ids = [f"item_{idx:04d}" for idx in
                     range(HOT_ITEM_COUNT, HOT_ITEM_COUNT + long_tail_count)]

    start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    interactions: list[dict[str, Any]] = []

    # Long-tail guarantee: each long-tail item gets exactly one interaction.
    for item_id in long_tail_ids:
        user_id = py_rng.choice(active_users)
        ts = start + timedelta(hours=int(py_rng.randint(0, 24 * 40)))
        interactions.append({
            "user_id": user_id,
            "item_id": item_id,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "interaction_type": py_rng.choice(interaction_types),
        })

    remaining = BASE_INTERACTIONS - len(interactions)
    hot_weights = np.ones(BASE_ITEMS, dtype=float)
    for idx in range(HOT_ITEM_COUNT):
        hot_weights[idx] = 8.0
    hot_weights = hot_weights / hot_weights.sum()

    for step in range(remaining):
        user_id = py_rng.choice(active_users)
        period = 24.0
        phase = np.sin(2.0 * np.pi * (step % period) / period)
        weights = hot_weights.copy()
        weights[:HOT_ITEM_COUNT] *= 1.0 + 0.8 * phase
        weights = weights / weights.sum()
        item_idx = int(np_rng.choice(BASE_ITEMS, p=weights))
        item_id = f"item_{item_idx:04d}"
        ts = start + timedelta(minutes=10 * step, seconds=int(py_rng.randint(0, 59)))
        interactions.append({
            "user_id": user_id,
            "item_id": item_id,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "interaction_type": py_rng.choice(interaction_types),
        })

    interactions.sort(key=lambda row: (row["user_id"], row["timestamp"], row["item_id"]))
    _inject_faulty_rows(interactions, items, interaction_types, py_rng)
    return items, interactions


def _inject_faulty_rows(
    interactions: list[dict[str, Any]],
    items: list[dict[str, Any]],
    interaction_types: list[str],
    py_rng: Any,
) -> None:
    # Stable users/items exist in every scale slice, so each fault survives
    # slicing and every scale exercises all four parser business codes.
    anchor = next(
        row for row in interactions
        if row["user_id"] == "user_0010" and row["item_id"] != "item_0001"
    ).copy()

    # E_LOG_INVALID: unknown interaction type and a malformed json line.
    interactions.append({
        "user_id": "user_0010",
        "item_id": "item_0001",
        "timestamp": "2026-02-15T00:00:00Z",
        "interaction_type": "teleport",
        "_faulty": True,
    })
    # E_ITEM_MISSING: item not present in dictionary.
    interactions.append({
        "user_id": "user_0010",
        "item_id": "item_9999",
        "timestamp": "2026-02-16T00:00:00Z",
        "interaction_type": interaction_types[0],
        "_faulty": True,
    })
    # E_LOG_DUPLICATE: exact copy of an existing row.
    duplicate = dict(anchor)
    duplicate["_faulty"] = True
    interactions.append(duplicate)
    # E_LOG_ORDER: same user as anchor but timestamp clearly earlier.
    interactions.append({
        "user_id": "user_0010",
        "item_id": "item_0002",
        "timestamp": "2026-01-02T00:00:00Z",
        "interaction_type": interaction_types[0],
        "_faulty": True,
    })
    # Dictionary-level E_LOG_INVALID: item with empty terms.
    items.append({"item_id": "item_8000", "terms": []})


def slice_for_scale(
    items: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
    n_users: int,
    n_items: int,
    n_interactions: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept_items = [
        row for row in items
        if _item_index(row["item_id"]) < n_items or row["item_id"] == "item_8000"
    ]
    allowed_users = {f"user_{idx:04d}" for idx in range(n_users)}
    allowed_items = {row["item_id"] for row in kept_items}
    sliced: list[dict[str, Any]] = []
    for row in interactions:
        if row.get("_faulty"):
            continue
        if row["user_id"] in allowed_users and row["item_id"] in allowed_items:
            sliced.append(row)
            if len(sliced) >= n_interactions:
                break
    # Keep faulty tail rows so every scale exercises the failure paths,
    # even when the interaction cap is small.
    for row in interactions:
        if row.get("_faulty"):
            clean = {key: value for key, value in row.items() if key != "_faulty"}
            sliced.append(clean)
    return kept_items, sliced


def _item_index(item_id: str) -> int:
    try:
        return int(item_id.split("_")[1])
    except (IndexError, ValueError):
        return 10 ** 9


def write_jsonl(path: str, rows: list[dict[str, Any]] | list[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, str):
                handle.write(row + "\n")
            else:
                handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def write_malformed_line(path: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"user_id": "user_0012", "item_id": "item_0003", broken json\n')


SCALE_PROFILES = [
    {"scale_id": "small", "n_users": 12, "n_items": 20, "n_interactions": 180},
    {"scale_id": "medium", "n_users": 25, "n_items": 40, "n_interactions": 480},
    {"scale_id": "large", "n_users": BASE_USERS, "n_items": BASE_ITEMS,
     "n_interactions": BASE_INTERACTIONS},
]


def generate_sample_workspace(sample_dir: str, config: dict[str, Any]) -> dict[str, Any]:
    seed = int(config["seed"])
    items, interactions = generate_base(seed, list(config["interaction_types"]))
    manifest: dict[str, Any] = {"seed": seed, "scales": []}

    for profile in SCALE_PROFILES:
        scale_dir = os.path.join(sample_dir, profile["scale_id"])
        os.makedirs(scale_dir, exist_ok=True)
        scale_items, scale_interactions = slice_for_scale(
            items, interactions,
            profile["n_users"], profile["n_items"], profile["n_interactions"],
        )
        dict_path = os.path.join(scale_dir, "items.jsonl")
        log_path = os.path.join(scale_dir, "interactions.jsonl")
        write_jsonl(dict_path, scale_items)
        write_jsonl(log_path, scale_interactions)
        write_malformed_line(log_path)
        manifest["scales"].append({
            **profile,
            "dictionary_path": dict_path,
            "log_path": log_path,
            "input_fingerprint": stable_hash(
                {"items": scale_items, "interactions": scale_interactions, "profile": profile}
            ),
        })

    with open(os.path.join(sample_dir, "scale_profiles.json"), "w", encoding="utf-8") as handle:
        json.dump(SCALE_PROFILES, handle, sort_keys=True, ensure_ascii=False, indent=2)
    with open(os.path.join(sample_dir, "generation_manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True, ensure_ascii=False, indent=2)
    return manifest
