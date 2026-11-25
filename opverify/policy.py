from __future__ import annotations

from copy import deepcopy
from typing import Any

from .errors import OpVerifyError


BOUNDARY_MODES = ("zero", "replicate", "reflect")
SELECTION_MODES = ("balanced", "largest_fit", "min_waste", "min_oob")

DEFAULT_BLOCK_POLICY: dict[str, Any] = {
    "candidates": [[4, 4], [8, 8]],
    "selection": "balanced",
    "boundary": "zero",
    "max_tile_elements": 4096,
}

PRESET_POLICIES: dict[str, dict[str, Any]] = {
    "tiny": {"candidates": [[2, 2], [3, 3]], "selection": "min_waste", "boundary": "zero"},
    "small": {"candidates": [[4, 4]], "selection": "largest_fit", "boundary": "zero"},
    "edge": {"candidates": [[3, 3], [5, 5]], "selection": "min_oob", "boundary": "replicate"},
    "reflect": {"candidates": [[4, 4], [6, 6]], "selection": "balanced", "boundary": "reflect"},
}


def _pair(value: Any, field: str) -> list[int]:
    if isinstance(value, bool):
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID", "block size must be an integer or pair", field=field
        )
    if isinstance(value, int):
        if value < 1:
            raise OpVerifyError(
                "E_BLOCK_POLICY_INVALID", "block size must be positive", field=field
            )
        return [value, value]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        if not all(isinstance(item, int) and not isinstance(item, bool) and item > 0 for item in value):
            raise OpVerifyError(
                "E_BLOCK_POLICY_INVALID",
                "block size pair must contain positive integers",
                field=field,
            )
        return [int(value[0]), int(value[1])]
    raise OpVerifyError(
        "E_BLOCK_POLICY_INVALID", "block size must be an integer or [height, width]", field=field
    )


def normalize_block_policy(value: Any) -> dict[str, Any]:
    if value is None:
        value = deepcopy(DEFAULT_BLOCK_POLICY)
    if isinstance(value, str):
        if value not in PRESET_POLICIES:
            raise OpVerifyError(
                "E_BLOCK_POLICY_INVALID",
                f"unknown block policy preset: {value}",
                field="$.block_policy",
            )
        value = deepcopy(PRESET_POLICIES[value])
    if not isinstance(value, dict):
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID",
            "block_policy must be an object or supported preset name",
            field="$.block_policy",
        )

    policy = deepcopy(DEFAULT_BLOCK_POLICY)
    policy.update({k: v for k, v in value.items() if k in DEFAULT_BLOCK_POLICY or k in {"candidates", "selection", "boundary"}})

    raw_candidates = value.get("candidates", value.get("block_sizes", policy["candidates"]))
    if not isinstance(raw_candidates, (list, tuple)) or not raw_candidates:
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID",
            "block_policy.candidates must be a non-empty list",
            field="$.block_policy.candidates",
        )
    candidates = [_pair(item, "$.block_policy.candidates") for item in raw_candidates]
    deduped: list[list[int]] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    policy["candidates"] = deduped

    selection = str(value.get("selection", policy["selection"]))
    if selection not in SELECTION_MODES:
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID",
            f"selection must be one of {SELECTION_MODES}",
            field="$.block_policy.selection",
        )
    policy["selection"] = selection

    boundary = str(value.get("boundary", policy["boundary"]))
    if boundary not in BOUNDARY_MODES:
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID",
            f"boundary must be one of {BOUNDARY_MODES}",
            field="$.block_policy.boundary",
        )
    policy["boundary"] = boundary

    max_elements = value.get("max_tile_elements", policy["max_tile_elements"])
    if not isinstance(max_elements, int) or isinstance(max_elements, bool) or max_elements < 1:
        raise OpVerifyError(
            "E_BLOCK_POLICY_INVALID",
            "max_tile_elements must be a positive integer",
            field="$.block_policy.max_tile_elements",
        )
    policy["max_tile_elements"] = max_elements
    return policy
