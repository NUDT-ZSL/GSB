"""Deterministic JSONL preference-pair loading and input digests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import content_hash
from .errors import E_DUPLICATE_PAIR_ID, E_INPUT_INVALID, BusinessError


REQUIRED_FIELDS = (
    "pair_id",
    "prompt",
    "chosen",
    "rejected",
    "annotator_group",
    "quality_flag",
)


def load_pairs(path: str | Path) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        handle = Path(path).open("r", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise BusinessError(E_INPUT_INVALID, f"cannot open input: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                pair = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    E_INPUT_INVALID,
                    "invalid JSONL line",
                    details={"line": line_number, "error": str(exc)},
                ) from exc
            if not isinstance(pair, dict):
                raise BusinessError(
                    E_INPUT_INVALID,
                    "each JSONL line must be an object",
                    details={"line": line_number},
                )
            missing = [field for field in REQUIRED_FIELDS if field not in pair]
            if missing:
                raise BusinessError(
                    E_INPUT_INVALID,
                    "pair is missing required fields",
                    details={"line": line_number, "missing": missing},
                )
            for field in REQUIRED_FIELDS:
                if not isinstance(pair[field], str):
                    raise BusinessError(
                        E_INPUT_INVALID,
                        f"{field} must be a string",
                        details={"line": line_number},
                    )
            pair_id = pair["pair_id"]
            if not pair_id:
                raise BusinessError(
                    E_INPUT_INVALID,
                    "pair_id must not be empty",
                    details={"line": line_number},
                )
            if pair_id in seen:
                raise BusinessError(
                    E_DUPLICATE_PAIR_ID,
                    "duplicate pair_id",
                    pair_ids=[pair_id],
                    details={"line": line_number},
                )
            seen.add(pair_id)
            pairs.append(pair)
    pairs.sort(key=lambda item: item["pair_id"])
    return pairs


def pair_digest(pair: dict[str, Any]) -> str:
    return content_hash(pair)


def input_digest(pairs: list[dict[str, Any]]) -> str:
    return content_hash(
        {
            "schema_version": "pair-dataset/1.0",
            "pairs": [
                pair_digest(pair) for pair in sorted(pairs, key=lambda item: item["pair_id"])
            ],
        }
    )


def pair_digests(pairs: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(pair["pair_id"]): pair_digest(pair)
        for pair in sorted(pairs, key=lambda item: item["pair_id"])
    }
