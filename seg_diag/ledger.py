"""Deterministic cross-version adjudication and semantic ledger construction."""

from __future__ import annotations

from typing import Any

import numpy as np

from .hashing import canonical_bytes, content_key
from .models import UnifiedConfig
from .normalization import normalize_v1, normalize_v2


LEDGER_FORMAT = "seg-diag-ledger/v1"
ADJUDICATION_RULE = {
    "source_priority": {"truth": 0, "v2": 1, "v1": 2},
    "same_tier_order": "instance_id_ascending",
    "empty_winner": "background",
}


def pixel_id(y: int, x: int) -> str:
    return f"P:{int(y)}:{int(x)}"


def region_id(y: int, x: int, region_size: int = 4) -> str:
    return f"R:{int(y) // region_size}:{int(x) // region_size}"


def _truth_candidates(
    y: int,
    x: int,
    templates: np.ndarray,
    template_instance_ids: np.ndarray,
    template_category_ids: np.ndarray,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index in range(templates.shape[0]):
        if int(templates[index, y, x]) != 0:
            candidates.append(
                {
                    "source": "truth",
                    "priority": 0,
                    "instance_id": int(template_instance_ids[index]),
                    "category_id": int(template_category_ids[index]),
                }
            )
    return candidates


def row_dependency_key(
    y: int,
    x: int,
    *,
    truth: list[dict[str, Any]],
    v1_instance: int,
    v1_config: UnifiedConfig,
    v2_raw_instance: int,
    v2_raw_category: int,
    v2_confidence: float,
    v2_config: UnifiedConfig,
    threshold_strategy: str,
    tolerance: float,
) -> str:
    v1_mapping = v1_config.mapping_for(v1_instance)
    v2_mapping = v2_config.mapping_for(v2_raw_instance)
    return content_key(
        {
            "engine": "seg-diag-row/v1",
            "pixel": [y, x],
            "rule": ADJUDICATION_RULE,
            "threshold_strategy": threshold_strategy,
            "tolerance": tolerance,
            "truth": sorted(
                [(item["instance_id"], item["category_id"]) for item in truth],
                key=lambda item: item[0],
            ),
            "v1": {
                "instance_id": int(v1_instance),
                "mapping": None
                if v1_mapping is None
                else [v1_mapping.instance_id, v1_mapping.category_id],
            },
            "v2": {
                "raw_instance_id": int(v2_raw_instance),
                "raw_category_id": int(v2_raw_category),
                "confidence": float(v2_confidence),
                "mapping": None
                if v2_mapping is None
                else [
                    v2_mapping.instance_id,
                    v2_mapping.category_id,
                    v2_mapping.confidence_threshold,
                ],
                "default_threshold": v2_config.default_confidence_threshold,
            },
        }
    )


def compute_pixel_row(
    y: int,
    x: int,
    arrays: dict[str, np.ndarray],
    v1_config: UnifiedConfig,
    v2_config: UnifiedConfig,
    *,
    v1_normalized: dict[str, np.ndarray] | None = None,
    v2_normalized: dict[str, np.ndarray] | None = None,
    threshold_strategy: str = "strict",
    tolerance: float = 1e-12,
    region_size: int = 4,
) -> dict[str, Any]:
    if v1_normalized is None:
        v1_normalized = normalize_v1(arrays["v1_instance"], v1_config)
    if v2_normalized is None:
        v2_normalized = normalize_v2(
            arrays["v2_category"],
            arrays["v2_instance"],
            arrays["v2_confidence"],
            v2_config,
            strategy=threshold_strategy,
            tolerance=tolerance,
        )

    truth = _truth_candidates(
        y,
        x,
        arrays["templates"],
        arrays["template_instance_ids"],
        arrays["template_category_ids"],
    )
    candidates = list(truth)
    if bool(v2_normalized["accepted"][y, x]):
        candidates.append(
            {
                "source": "v2",
                "priority": 1,
                "instance_id": int(v2_normalized["instance"][y, x]),
                "category_id": int(v2_normalized["category"][y, x]),
                "confidence": float(v2_normalized["confidence"][y, x]),
                "threshold": float(v2_normalized["thresholds"][y, x]),
                "in_band": bool(v2_normalized["in_band"][y, x]),
            }
        )
    if bool(v1_normalized["accepted"][y, x]):
        candidates.append(
            {
                "source": "v1",
                "priority": 2,
                "instance_id": int(v1_normalized["instance"][y, x]),
                "category_id": int(v1_normalized["category"][y, x]),
                "confidence": 1.0,
            }
        )

    ordered = sorted(candidates, key=lambda item: (item["priority"], item["instance_id"]))
    winner = ordered[0] if ordered else {"source": "none", "instance_id": 0, "category_id": 0}
    dependency_key = row_dependency_key(
        y,
        x,
        truth=truth,
        v1_instance=int(arrays["v1_instance"][y, x]),
        v1_config=v1_config,
        v2_raw_instance=int(arrays["v2_instance"][y, x]),
        v2_raw_category=int(arrays["v2_category"][y, x]),
        v2_confidence=float(arrays["v2_confidence"][y, x]),
        v2_config=v2_config,
        threshold_strategy=threshold_strategy,
        tolerance=tolerance,
    )
    return {
        "row_id": pixel_id(y, x),
        "pixel": {"y": int(y), "x": int(x)},
        "region_id": region_id(y, x, region_size),
        "dependency_key": dependency_key,
        "winner": {
            "source": winner["source"],
            "instance_id": int(winner["instance_id"]),
            "category_id": int(winner["category_id"]),
        },
        "candidates": ordered,
        "expected_gt": {
            "instance_id": int(arrays["gt_instance"][y, x]),
            "category_id": int(arrays["gt_category"][y, x]),
        },
    }


def iter_pixel_coordinates(height: int, width: int) -> list[tuple[int, int]]:
    return [(y, x) for y in range(height) for x in range(width)]


def build_ledger(
    arrays: dict[str, np.ndarray],
    v1_config: UnifiedConfig,
    v2_config: UnifiedConfig,
    *,
    seed: int,
    threshold_strategy: str = "strict",
    tolerance: float = 1e-12,
    region_size: int = 4,
    coordinates: list[tuple[int, int]] | None = None,
    existing_rows: dict[str, dict[str, Any]] | None = None,
    ledger_version: int = 1,
) -> dict[str, Any]:
    height, width = arrays["gt_instance"].shape
    v1_normalized = normalize_v1(arrays["v1_instance"], v1_config)
    v2_normalized = normalize_v2(
        arrays["v2_category"],
        arrays["v2_instance"],
        arrays["v2_confidence"],
        v2_config,
        strategy=threshold_strategy,
        tolerance=tolerance,
    )
    coords = coordinates if coordinates is not None else iter_pixel_coordinates(height, width)
    rows_by_id: dict[str, dict[str, Any]] = dict(existing_rows or {})
    computed_rows: list[str] = []
    reused_rows: list[str] = []

    for y, x in coords:
        row_id = pixel_id(y, x)
        candidate_key = row_dependency_key(
            y,
            x,
            truth=_truth_candidates(
                y,
                x,
                arrays["templates"],
                arrays["template_instance_ids"],
                arrays["template_category_ids"],
            ),
            v1_instance=int(arrays["v1_instance"][y, x]),
            v1_config=v1_config,
            v2_raw_instance=int(arrays["v2_instance"][y, x]),
            v2_raw_category=int(arrays["v2_category"][y, x]),
            v2_confidence=float(arrays["v2_confidence"][y, x]),
            v2_config=v2_config,
            threshold_strategy=threshold_strategy,
            tolerance=tolerance,
        )
        cached = rows_by_id.get(row_id)
        if cached is not None and cached.get("dependency_key") == candidate_key:
            reused_rows.append(row_id)
            continue

        row = compute_pixel_row(
            y,
            x,
            arrays,
            v1_config,
            v2_config,
            v1_normalized=v1_normalized,
            v2_normalized=v2_normalized,
            threshold_strategy=threshold_strategy,
            tolerance=tolerance,
            region_size=region_size,
        )
        row["dependency_key"] = candidate_key
        rows_by_id[row_id] = row
        computed_rows.append(row_id)

    all_coordinates = iter_pixel_coordinates(height, width)
    rows = [rows_by_id[pixel_id(y, x)] for y, x in all_coordinates]
    ledger = {
        "format": LEDGER_FORMAT,
        "ledger_version": int(ledger_version),
        "seed": int(seed),
        "height": int(height),
        "width": int(width),
        "rule": ADJUDICATION_RULE,
        "threshold_strategy": threshold_strategy,
        "tolerance": float(tolerance),
        "region_size": int(region_size),
        "config_checksums": {
            "v1": v1_config.checksum,
            "v2": v2_config.checksum,
        },
        "rows": rows,
    }
    ledger["checksum"] = content_key(_ledger_checksum_payload(ledger))
    ledger["byte_checksum"] = content_key(canonical_bytes(_ledger_bytes_payload(ledger)))
    ledger["_computed_rows"] = computed_rows
    ledger["_reused_rows"] = reused_rows
    return ledger


def _ledger_checksum_payload(ledger: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in ledger.items() if not key.startswith("_") and key not in {"checksum", "byte_checksum"}}


def _ledger_bytes_payload(ledger: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in ledger.items() if not key.startswith("_")}


def ledger_canonical_bytes(ledger: dict[str, Any]) -> bytes:
    return canonical_bytes(_ledger_bytes_payload(ledger)) + b"\n"


def assemble_ledger(
    rows_by_id: dict[str, dict[str, Any]],
    *,
    arrays: dict[str, np.ndarray],
    seed: int,
    v1_checksum: str,
    v2_checksum: str,
    threshold_strategy: str = "strict",
    tolerance: float = 1e-12,
    region_size: int = 4,
    ledger_version: int = 1,
) -> dict[str, Any]:
    height, width = arrays["gt_instance"].shape
    rows = [
        rows_by_id[pixel_id(y, x)]
        for y, x in iter_pixel_coordinates(height, width)
        if pixel_id(y, x) in rows_by_id
    ]
    ledger = {
        "format": LEDGER_FORMAT,
        "ledger_version": int(ledger_version),
        "seed": int(seed),
        "height": int(height),
        "width": int(width),
        "rule": ADJUDICATION_RULE,
        "threshold_strategy": threshold_strategy,
        "tolerance": float(tolerance),
        "region_size": int(region_size),
        "config_checksums": {"v1": v1_checksum, "v2": v2_checksum},
        "rows": rows,
    }
    ledger["checksum"] = content_key(_ledger_checksum_payload(ledger))
    ledger["byte_checksum"] = content_key(canonical_bytes(_ledger_bytes_payload(ledger)))
    return ledger


def verify_ledger_checksum(ledger: dict[str, Any]) -> bool:
    expected = content_key(_ledger_checksum_payload(ledger))
    return expected == ledger.get("checksum")
