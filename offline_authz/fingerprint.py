"""Fixed-point fingerprint alignment and threshold-safe comparison."""

from __future__ import annotations

from typing import Any

from .codes import E_INVALID_FINGERPRINT_COMPONENT

CANONICAL_DECIMALS = 6


def is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def align_fixed_point(
    value: int, source_decimals: int, canonical_decimals: int = CANONICAL_DECIMALS
) -> dict[str, int]:
    """Align an unsigned fixed-point integer to the canonical scale.

    The canonical value is the floor of the exact converted value.  The
    returned lower/upper interval therefore contains the exact value.
    """

    if not is_nonneg_int(value):
        raise ValueError(E_INVALID_FINGERPRINT_COMPONENT)
    if not is_nonneg_int(source_decimals) or not is_nonneg_int(canonical_decimals):
        raise ValueError(E_INVALID_FINGERPRINT_COMPONENT)

    if source_decimals <= canonical_decimals:
        canonical = value * (10 ** (canonical_decimals - source_decimals))
        return {
            "canonical_value": canonical,
            "lower": canonical,
            "upper": canonical,
            "error_bound_canonical_units": 0,
        }

    divisor = 10 ** (source_decimals - canonical_decimals)
    canonical = value // divisor
    remainder = value % divisor
    upper = canonical + 1 if remainder else canonical
    return {
        "canonical_value": canonical,
        "lower": canonical,
        "upper": upper,
        "error_bound_canonical_units": 1 if remainder else 0,
    }


def decimal_error_string(error_bound_units: int, decimals: int) -> str:
    width = max(1, decimals)
    integer_part = error_bound_units // 10**decimals
    fraction = error_bound_units % 10**decimals
    return f"{integer_part}.{fraction:0{width}d}"


def normalize_fingerprint(
    platform: str,
    platform_decimals: int,
    series: list[dict[str, Any]],
    canonical_decimals: int = CANONICAL_DECIMALS,
) -> dict[str, Any]:
    """Return deterministic canonical points, key and alignment bound."""

    points: list[dict[str, Any]] = []
    max_error = 0
    for point_index, point in enumerate(series):
        point_seq = point.get("seq", point_index + 1)
        components = point.get("components", {})
        normalized_components: list[dict[str, Any]] = []
        if isinstance(components, dict):
            component_items = sorted(components.items(), key=lambda item: str(item[0]))
        else:
            component_items = [
                (item.get("name"), item.get("value"))
                for item in components
                if isinstance(item, dict)
            ]
        for name, value in component_items:
            if not isinstance(name, str) or not name or not is_nonneg_int(value):
                raise ValueError(E_INVALID_FINGERPRINT_COMPONENT)
            aligned = align_fixed_point(value, platform_decimals, canonical_decimals)
            max_error = max(max_error, aligned["error_bound_canonical_units"])
            normalized_components.append(
                {
                    "name": name,
                    "canonical_value": aligned["canonical_value"],
                    "lower": aligned["lower"],
                    "upper": aligned["upper"],
                    "error_bound": aligned["error_bound_canonical_units"],
                }
            )
        points.append(
            {
                "seq": point_seq,
                "components": sorted(normalized_components, key=lambda item: item["name"]),
            }
        )

    points.sort(key=lambda point: point["seq"])
    key_payload = {
        "algorithm": "fixed-point-v1",
        "canonical_decimals": canonical_decimals,
        "platform": platform,
        "points": points,
    }
    from .jsonio import sha256_text

    digest = sha256_text(key_payload)
    return {
        "algorithm": "fixed-point-v1",
        "platform": platform,
        "platform_decimals": platform_decimals,
        "canonical_decimals": canonical_decimals,
        "points": points,
        "error_bound_canonical_units": max_error,
        "error_bound_decimal": decimal_error_string(max_error, canonical_decimals),
        "canonical_fingerprint_key": f"fpv1:sha256:{digest}",
    }


def flatten_points(points: list[dict[str, Any]]) -> dict[tuple[Any, str], dict[str, int]]:
    flat: dict[tuple[Any, str], dict[str, int]] = {}
    for point_index, point in enumerate(points or []):
        seq = point.get("seq", point_index + 1)
        components = point.get("components", [])
        if isinstance(components, dict):
            iterator = sorted(components.items(), key=lambda item: str(item[0]))
            for name, value in iterator:
                if isinstance(value, dict):
                    flat[(seq, name)] = {
                        "value": int(value.get("canonical_value", value.get("value", 0))),
                        "lower": int(value.get("lower", value.get("canonical_value", value.get("value", 0)))),
                        "upper": int(value.get("upper", value.get("canonical_value", value.get("value", 0)))),
                    }
                else:
                    flat[(seq, name)] = {"value": int(value), "lower": int(value), "upper": int(value)}
        else:
            for component in components:
                name = component["name"]
                value = int(component.get("canonical_value", component.get("value")))
                flat[(seq, name)] = {
                    "value": value,
                    "lower": int(component.get("lower", value)),
                    "upper": int(component.get("upper", value)),
                }
    return flat


def compare_canonical_fingerprints(
    left: dict[str, Any],
    right: dict[str, Any],
    threshold_canonical_units: int,
) -> dict[str, Any]:
    """Compare canonical vectors with conservative uncertainty intervals."""

    left_key = left.get("canonical_fingerprint_key")
    right_key = right.get("canonical_fingerprint_key")
    base = {
        "left_key": left_key,
        "right_key": right_key,
        "threshold_canonical_units": threshold_canonical_units,
        "canonical_decimals": left.get("canonical_decimals", CANONICAL_DECIMALS),
    }
    if left_key and left_key == right_key:
        return {
            **base,
            "comparison": "EXACT_MATCH",
            "same_device": True,
            "distance_canonical_units": 0,
            "lower_bound_canonical_units": 0,
            "upper_bound_canonical_units": 0,
            "error_bound_canonical_units": 0,
        }

    right_points = right.get("points") or right.get("canonical_vector") or []
    left_flat = flatten_points(left.get("points", []))
    right_flat = flatten_points(right_points)
    if not left_flat or set(left_flat) != set(right_flat):
        return {
            **base,
            "comparison": "MISMATCH",
            "same_device": False,
            "reason": "incompatible_components",
            "left_coordinates": sorted(f"{seq}:{name}" for seq, name in left_flat),
            "right_coordinates": sorted(f"{seq}:{name}" for seq, name in right_flat),
        }

    observed_distance = 0
    lower_distance = 0
    upper_distance = 0
    for coordinate in sorted(left_flat):
        lv = left_flat[coordinate]
        rv = right_flat[coordinate]
        observed_distance = max(observed_distance, abs(lv["value"] - rv["value"]))
        interval_low = max(lv["lower"], rv["lower"])
        interval_high = min(lv["upper"], rv["upper"])
        if interval_low <= interval_high:
            coordinate_lower = 0
        else:
            coordinate_lower = max(
                lv["lower"] - rv["upper"], rv["lower"] - lv["upper"]
            )
        coordinate_upper = max(
            abs(lv["lower"] - rv["upper"]),
            abs(lv["upper"] - rv["lower"]),
        )
        lower_distance = max(lower_distance, coordinate_lower)
        upper_distance = max(upper_distance, coordinate_upper)

    left_error = int(left.get("error_bound_canonical_units", 0) or 0)
    right_error = int(right.get("error_bound_canonical_units", 0) or 0)
    combined_error = left_error + right_error
    threshold = int(threshold_canonical_units)
    if upper_distance <= threshold:
        comparison = "MATCH"
        same_device = True
    elif lower_distance > threshold:
        comparison = "MISMATCH"
        same_device = False
    else:
        comparison = "AMBIGUOUS"
        same_device = False

    decimals = int(base["canonical_decimals"])
    return {
        **base,
        "comparison": comparison,
        "same_device": same_device,
        "distance_canonical_units": observed_distance,
        "lower_bound_canonical_units": lower_distance,
        "upper_bound_canonical_units": upper_distance,
        "combined_alignment_error_bound": combined_error,
        "distance_decimal": decimal_error_string(observed_distance, decimals),
        "threshold_decimal": decimal_error_string(threshold, decimals),
        "error_bound_decimal": decimal_error_string(combined_error, decimals),
    }
