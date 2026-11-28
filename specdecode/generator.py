"""Deterministic synthetic workload generator for the public-transport stream."""

from __future__ import annotations

import os
from typing import Any

from .model import (
    FALLBACK_RULE,
    MARKER_BY_CLASS,
    SPECIAL_IDS,
    TARGET_ONLY_IDS,
    UNK_ID,
    build_token_mapping,
    stable_u64,
)
from .schema import (
    canonical_fingerprint,
    dump_json,
    make_canonical_dataset,
    render_schema,
)


DEFAULT_SEED = 20261128
DEFAULT_TICK_MS = 2
DEFAULT_REQUEST_COUNT = 24

_CLASS_BODIES = {
    "template": (8, 9, 10, 11, 12, 13),
    "reroute": (14, 15, 16, 17, 18, 19, 20, 21, 22, 23),
    "transfer": (24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35),
}


def _special_positions(normalized_ids: list[int]) -> dict[str, list[int]]:
    by_id = {token_id: name for name, token_id in SPECIAL_IDS.items()}
    positions: dict[str, list[int]] = {}
    for position, token_id in enumerate(normalized_ids):
        name = by_id.get(token_id)
        if name is not None:
            positions.setdefault(name, []).append(position)
    return {name: positions[name] for name in SPECIAL_IDS if name in positions}


def _request_class(seed: int, index: int) -> str:
    order = ("template", "reroute", "transfer")
    offset = stable_u64("class-order", seed) % len(order)
    return order[(index + offset) % len(order)]


def _body_for_request(seed: int, index: int, request_class: str) -> list[int]:
    pool = _CLASS_BODIES[request_class]
    if request_class == "template":
        width = 5 + stable_u64("template-width", seed, index) % 3
        start = stable_u64("template-start", seed, index) % 2
        return [pool[(start + offset) % len(pool)] for offset in range(width)]

    width = 8 + stable_u64("body-width", seed, index) % 6
    return [
        pool[stable_u64("body", seed, index, offset) % len(pool)]
        for offset in range(width)
    ]


def _build_request(
    seed: int,
    index: int,
    mapping: dict[str, Any],
) -> dict[str, Any]:
    request_class = _request_class(seed, index)
    internal_to_target = {
        int(key): int(value)
        for key, value in dict(mapping["draft_to_target"]).items()
    }
    target_to_internal = {
        int(key): int(value)
        for key, value in dict(mapping["target_to_draft"]).items()
    }

    body = _body_for_request(seed, index, request_class)
    internal_prompt = [
        SPECIAL_IDS["bos"],
        MARKER_BY_CLASS[request_class],
        *body,
        SPECIAL_IDS["ctx_end"],
    ]
    raw_target_ids = [internal_to_target[token_id] for token_id in internal_prompt]

    unmappable_positions: list[int] = []
    if index % 7 == 0:
        position = 3
        raw_target_ids[position] = TARGET_ONLY_IDS[
            stable_u64("unmapped", seed, index) % len(TARGET_ONLY_IDS)
        ]
        unmappable_positions.append(position)
    if index % 14 == 0 and len(raw_target_ids) > 6:
        position = len(raw_target_ids) - 2
        raw_target_ids[position] = TARGET_ONLY_IDS[
            stable_u64("unmapped-tail", seed, index) % len(TARGET_ONLY_IDS)
        ]
        unmappable_positions.append(position)
    unmappable_positions.sort()

    normalized_ids = [
        target_to_internal.get(raw_id, UNK_ID) for raw_id in raw_target_ids
    ]
    special_positions = _special_positions(normalized_ids)

    arrival_tick = (
        index * 4 + stable_u64("arrival", seed, index) % 3
        if index < 14
        else 56 + (index - 14) * 24 + stable_u64("arrival", seed, index) % 5
    )
    deadline_tick = arrival_tick + 190 + stable_u64("deadline", seed, index) % 70
    max_new_tokens = 6 + stable_u64("max-new", seed, index) % 9
    tenant_id = f"tenant-{(stable_u64('tenant', seed, index) % 4) + 1}"

    return {
        "request_id": f"req-{index + 1:04d}",
        "request_class": request_class,
        "tenant_id": tenant_id,
        "arrival_tick": arrival_tick,
        "deadline_tick": deadline_tick,
        "max_new_tokens": max_new_tokens,
        "prompt_target_ids": raw_target_ids,
        "truth": {
            "normalized_internal_ids": normalized_ids,
            "normalized_length": len(normalized_ids),
            "special_positions": special_positions,
            "unmappable_positions": unmappable_positions,
            "mapping_complete": not unmappable_positions,
        },
    }


def make_policy(
    policy_id: str,
    width: int,
    threshold_bps: int,
    budget_ticks: int = 140,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "candidate_length": int(width),
        "acceptance_threshold_bps": int(threshold_bps),
        "fallback_rule": FALLBACK_RULE,
        "latency_budget_ticks": int(budget_ticks),
    }


def default_policies() -> list[dict[str, Any]]:
    return [
        make_policy("balanced-w4-t8000", width=4, threshold_bps=8000),
        make_policy("wide-w8-t5000", width=8, threshold_bps=5000),
        make_policy("strict-w6-t9500", width=6, threshold_bps=9500),
    ]


def generate_canonical(
    seed: int = DEFAULT_SEED,
    request_count: int = DEFAULT_REQUEST_COUNT,
    tick_ms: int = DEFAULT_TICK_MS,
) -> dict[str, Any]:
    if request_count <= 0:
        raise ValueError("request_count must be positive")
    mapping = build_token_mapping(seed)
    requests = [
        _build_request(seed, index, mapping) for index in range(request_count)
    ]
    return make_canonical_dataset(
        seed=seed,
        tick_ms=tick_ms,
        requests=requests,
        policies=default_policies(),
    )


def generate_dataset_files(
    out_dir: str,
    seed: int = DEFAULT_SEED,
    request_count: int = DEFAULT_REQUEST_COUNT,
    tick_ms: int = DEFAULT_TICK_MS,
) -> dict[str, Any]:
    canonical = generate_canonical(
        seed=seed, request_count=request_count, tick_ms=tick_ms
    )
    os.makedirs(out_dir, exist_ok=True)
    files: dict[str, str] = {}
    for version in (1, 2, 3):
        filename = f"dataset_v{version}.json"
        dump_json(os.path.join(out_dir, filename), render_schema(canonical, version))
        files[str(version)] = filename

    manifest = {
        "format": "specdecode-generated-datasets-v1",
        "seed": seed,
        "tick_ms": tick_ms,
        "request_count": request_count,
        "policy_count": len(canonical["policies"]),
        "canonical_fingerprint": canonical_fingerprint(canonical),
        "schemas": files,
    }
    dump_json(os.path.join(out_dir, "manifest.json"), manifest)
    return manifest


__all__ = [
    "DEFAULT_REQUEST_COUNT",
    "DEFAULT_SEED",
    "DEFAULT_TICK_MS",
    "default_policies",
    "generate_canonical",
    "generate_dataset_files",
    "make_policy",
]
