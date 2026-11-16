"""Joint nnz-band and B-dynamic-range sensitivity tables."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from .errors import ConfigError


def _parse_bands(raw: Any, *, name: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{name} must be a non-empty list", stage="layer-config")
    bands: list[dict[str, Any]] = []
    previous_upper: float | None = 0.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"{name}[{index}] must be an object", stage="layer-config")
        lower = float(item.get("lower", 0.0))
        upper_raw = item.get("upper")
        upper = None if upper_raw is None else float(upper_raw)
        if not math.isfinite(lower) or lower < 0.0:
            raise ConfigError(f"{name}[{index}].lower is invalid", stage="layer-config")
        if upper is not None and (not math.isfinite(upper) or upper <= lower):
            raise ConfigError(f"{name}[{index}] upper is invalid", stage="layer-config")
        if index == 0 and lower != 0.0:
            raise ConfigError(f"{name} must begin at lower=0", stage="layer-config")
        if previous_upper is not None and lower < previous_upper:
            raise ConfigError(f"{name} bands overlap or are unsorted", stage="layer-config")
        bands.append({"lower": lower, "upper": upper})
        previous_upper = upper
    if bands[-1]["upper"] is not None:
        raise ConfigError(f"{name} final band must be open-ended", stage="layer-config")
    return bands


def _band_index(value: float, bands: list[dict[str, Any]], *, name: str) -> int:
    for index, band in enumerate(bands):
        upper = band["upper"]
        if value >= band["lower"] and (upper is None or value < upper):
            return index
    raise ConfigError(
        f"{name} value is outside configured bands",
        stage="layer-assignment",
        details={"value": value, "bands": bands},
    )


def _band_text(band: dict[str, Any]) -> str:
    upper = "inf" if band["upper"] is None else f"{band['upper']:.0f}"
    return f"[{band['lower']:.0f},{upper})"


def _summary_for_group(
    elements: list[dict[str, Any]], *, group_key: str, label: str
) -> dict[str, Any]:
    if not elements:
        raise ValueError("layer group cannot be empty")
    exceeded = [element for element in elements if element["exceeds_tolerance"]]
    factor_source = exceeded if exceeded else elements
    factor_counts = Counter(element["dominant_source"] for element in factor_source)
    dominant_factor = sorted(factor_counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    max_relative = max(float(element["relative_error"]) for element in elements)
    max_absolute = max(float(element["absolute_error"]) for element in elements)
    mean_relative = sum(float(element["relative_error"]) for element in elements) / len(elements)
    mean_share = sum(float(element["top1_abs_share"]) for element in elements) / len(elements)
    mean_cumulative = (
        sum(
            float(element["top_contributions"][-1]["cumulative_share"])
            for element in elements
            if element["top_contributions"]
        )
        / max(1, sum(1 for element in elements if element["top_contributions"]))
    )
    lower = float(elements[0]["_nnz_band"]["lower"])
    upper = elements[0]["_nnz_band"]["upper"]
    mag_lower = float(elements[0]["_magnitude_band"]["lower"])
    mag_upper = elements[0]["_magnitude_band"]["upper"]
    if not exceeded:
        conclusion = "NO_TOLERANCE_VIOLATION"
    else:
        conclusion = f"DOMINANT_{dominant_factor}"
    summary = {
        group_key: label,
        "element_count": len(elements),
        "violation_count": len(exceeded),
        "violation_ratio": len(exceeded) / len(elements),
        "max_relative_error": max_relative,
        "max_absolute_error": max_absolute,
        "mean_relative_error": mean_relative,
        "mean_top1_contribution_share": mean_share,
        "mean_top_k_cumulative_share": mean_cumulative,
        "dominant_source": dominant_factor,
        "dominant_source_counts": dict(sorted(factor_counts.items())),
        "conclusion": conclusion,
        "violating_element_ids": [element["element_id"] for element in exceeded],
        "sample_element_ids": [element["element_id"] for element in elements[: min(5, len(elements))]],
        "_nnz_lower": lower,
        "_nnz_upper": upper,
        "_magnitude_lower": mag_lower,
        "_magnitude_upper": mag_upper,
    }
    return summary


def analyze_layers(
    elements: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    layer_config = config.get("layers", {})
    nnz_bands = _parse_bands(layer_config.get("nnz_bands"), name="layers.nnz_bands")
    magnitude_bands = _parse_bands(
        layer_config.get("magnitude_ratio_bands"), name="layers.magnitude_ratio_bands"
    )
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    nnz_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    magnitude_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for element in elements:
        nnz_index = _band_index(float(element["nnz"]), nnz_bands, name="nnz")
        magnitude_index = _band_index(
            float(element["b_magnitude_ratio"]), magnitude_bands, name="magnitude_ratio"
        )
        nnz_band = nnz_bands[nnz_index]
        magnitude_band = magnitude_bands[magnitude_index]
        layer_id = (
            f"L-nnz{_band_text(nnz_band)}-mag{_band_text(magnitude_band)}"
        )
        element["_nnz_band"] = nnz_band
        element["_magnitude_band"] = magnitude_band
        element["nnz_band"] = _band_text(nnz_band)
        element["magnitude_ratio_band"] = _band_text(magnitude_band)
        element["layer_id"] = layer_id
        groups[layer_id].append(element)
        nnz_groups[nnz_index].append(element)
        magnitude_groups[magnitude_index].append(element)

    joint = [
        _summary_for_group(group, group_key="layer_id", label=layer_id)
        for layer_id, group in groups.items()
    ]
    joint.sort(
        key=lambda item: (
            -item["max_relative_error"],
            item["_nnz_lower"],
            item["_magnitude_lower"],
            item["layer_id"],
        )
    )
    for item in joint:
        item["nnz_band"] = _band_text(
            {"lower": item["_nnz_lower"], "upper": item["_nnz_upper"]}
        )
        item["magnitude_ratio_band"] = _band_text(
            {"lower": item["_magnitude_lower"], "upper": item["_magnitude_upper"]}
        )
        item.pop("_nnz_lower", None)
        item.pop("_nnz_upper", None)
        item.pop("_magnitude_lower", None)
        item.pop("_magnitude_upper", None)

    marginal_nnz = []
    for index in sorted(nnz_groups):
        group = nnz_groups[index]
        summary = _summary_for_group(group, group_key="nnz_band", label=_band_text(nnz_bands[index]))
        summary.pop("_nnz_lower", None)
        summary.pop("_nnz_upper", None)
        summary.pop("_magnitude_lower", None)
        summary.pop("_magnitude_upper", None)
        marginal_nnz.append(summary)
    marginal_nnz.sort(key=lambda item: (-item["max_relative_error"], item["nnz_band"]))

    marginal_magnitude = []
    for index in sorted(magnitude_groups):
        group = magnitude_groups[index]
        summary = _summary_for_group(
            group, group_key="magnitude_ratio_band", label=_band_text(magnitude_bands[index])
        )
        summary.pop("_nnz_lower", None)
        summary.pop("_nnz_upper", None)
        summary.pop("_magnitude_lower", None)
        summary.pop("_magnitude_upper", None)
        marginal_magnitude.append(summary)
    marginal_magnitude.sort(
        key=lambda item: (-item["max_relative_error"], item["magnitude_ratio_band"])
    )

    for element in elements:
        element.pop("_nnz_band", None)
        element.pop("_magnitude_band", None)

    return {
        "band_definitions": {
            "nnz_intervals": [_band_text(band) for band in nnz_bands],
            "magnitude_ratio_intervals": [_band_text(band) for band in magnitude_bands],
            "semantics": "lower-inclusive, upper-exclusive; ratio=max|B[k,j]|/min|B[k,j]| over row nonzeros",
            "sort_order": "max_relative_error descending, ties by nnz lower then magnitude lower ascending",
        },
        "joint_layers": joint,
        "marginal_nnz_layers": marginal_nnz,
        "marginal_magnitude_layers": marginal_magnitude,
    }
