"""Normalize the three geospatial feature schemas into one internal model."""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from .errors import BusinessError, invalid_input
from .models import (
    BandCombination,
    FrequencyBand,
    NormalizedBlock,
    NormalizedRequest,
)

_UNIT_METERS = {
    "m",
    "meter",
    "meters",
    "metre",
    "metres",
    "geo",
    "geographic",
    "米",
}
_UNIT_PIXELS = {
    "px",
    "pixel",
    "pixels",
    "grid",
    "index",
    "indices",
    "raster",
    "像素",
    "像元",
}


def _compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _lookup(mapping: Mapping[str, Any], *names: str) -> tuple[Any, str | None]:
    if not isinstance(mapping, Mapping):
        return None, None
    normalized = {_compact_key(str(key)): key for key in mapping.keys()}
    for name in names:
        key = normalized.get(_compact_key(name))
        if key is not None:
            return mapping[key], str(key)
    return None, None


def _number(value: Any, path: str, location: dict[str, Any]) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise invalid_input(f"{path} must be numeric", location) from None
    if not math.isfinite(result):
        raise invalid_input(f"{path} must be finite", location)
    return result


def _integer(value: Any, path: str, location: dict[str, Any]) -> int:
    number = _number(value, path, location)
    if not number.is_integer():
        raise invalid_input(f"{path} must be an integer", location)
    return int(number)


def _pair_from_value(
    value: Any, path: str, location: dict[str, Any]
) -> tuple[float, float]:
    if isinstance(value, Mapping):
        x, _ = _lookup(value, "x", "east", "easting", "col", "longitude", "lon")
        y, _ = _lookup(value, "y", "north", "northing", "row", "latitude", "lat")
        if x is None or y is None:
            raise invalid_input(f"{path} needs x and y", location)
        return _number(x, f"{path}.x", location), _number(y, f"{path}.y", location)
    if isinstance(value, str):
        pieces = [piece for piece in re.split(r"[,\s]+", value.strip()) if piece]
        if len(pieces) != 2:
            raise invalid_input(f"{path} needs two values", location)
        return (
            _number(pieces[0], f"{path}[0]", location),
            _number(pieces[1], f"{path}[1]", location),
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        values = list(value)
        if len(values) == 1:
            return _number(values[0], path, location), _number(values[0], path, location)
        if len(values) >= 2:
            return (
                _number(values[0], f"{path}[0]", location),
                _number(values[1], f"{path}[1]", location),
            )
    number = _number(value, path, location)
    return number, number


def _normalize_schema_version(value: Any) -> str:
    text = str(value).strip().lower()
    match = re.search(r"(?:v|version|schema)[-_ ]*([123])", text)
    if match:
        return f"v{match.group(1)}"
    if text in {"1", "2", "3"}:
        return f"v{text}"
    if text in {"a", "schema-a", "schemaa"}:
        return "v1"
    if text in {"b", "schema-b"}:
        return "v2"
    if text in {"c", "schema-c"}:
        return "v3"
    return text


def _normalize_unit(value: Any) -> str:
    text = str(value).strip().lower()
    if text in _UNIT_METERS:
        return "meters"
    if text in _UNIT_PIXELS:
        return "pixels"
    return "unknown"


def _normalize_anchor(value: Any) -> str:
    text = str(value).strip().lower()
    if text in {"center", "centre", "pixel_center", "pixelcenter", "cell_center"}:
        return "center"
    if text in {"corner", "pixel_corner", "topleft", "top_left", "origin"}:
        return "corner"
    return "center"


def _block_containers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    blocks, _ = _lookup(payload, "blocks", "feature_blocks", "tiles")
    if isinstance(blocks, list):
        for item in blocks:
            if not isinstance(item, Mapping):
                raise invalid_input("blocks entries must be objects")
            containers.append(dict(item))

    versions, _ = _lookup(payload, "versions", "schema_versions", "models")
    if isinstance(versions, Mapping):
        for version_name, value in versions.items():
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, Mapping):
                        raise invalid_input("version block entries must be objects")
                    block = dict(item)
                    block.setdefault("schema_version", version_name)
                    containers.append(block)
            elif isinstance(value, Mapping):
                nested, _ = _lookup(value, "blocks", "feature_blocks", "tiles")
                if isinstance(nested, list):
                    for item in nested:
                        if not isinstance(item, Mapping):
                            raise invalid_input("version block entries must be objects")
                        block = dict(item)
                        block.setdefault("schema_version", version_name)
                        containers.append(block)
                else:
                    block = dict(value)
                    block.setdefault("schema_version", version_name)
                    containers.append(block)

    for key, value in payload.items():
        normalized = _normalize_schema_version(key)
        if normalized not in {"v1", "v2", "v3"} or key in {"versions", "schema_versions"}:
            continue
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    block = dict(item)
                    block.setdefault("schema_version", key)
                    containers.append(block)
        elif isinstance(value, Mapping):
            nested, _ = _lookup(value, "blocks", "feature_blocks", "tiles")
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, Mapping):
                        block = dict(item)
                        block.setdefault("schema_version", key)
                        containers.append(block)
            else:
                block = dict(value)
                block.setdefault("schema_version", key)
                containers.append(block)

    if not containers:
        raise invalid_input("request must contain at least one feature block")
    return containers


def _find_origin(
    raw: Mapping[str, Any], location: dict[str, Any]
) -> tuple[tuple[float, float], tuple[str, Any] | None, str | None]:
    candidates: list[tuple[str, tuple[float, float]]] = []
    unit_hint: tuple[str, Any] | None = None
    order_value, order_field = _lookup(
        raw,
        "origin_order",
        "coordinate_order",
        "geo_order",
        "axis_order",
    )

    def ordered_pair(value: Any, path: str, inherited_order: Any = None) -> tuple[float, float]:
        pair = _pair_from_value(value, path, location)
        declared_order = inherited_order
        if isinstance(value, Mapping):
            nested_order, _ = _lookup(
                value, "order", "axis_order", "coordinate_order"
            )
            if nested_order is not None:
                declared_order = nested_order
        if declared_order is None or isinstance(value, Mapping):
            return pair
        if isinstance(declared_order, str):
            normalized = declared_order.strip().lower()
            if normalized.startswith("yx") or normalized.startswith("rowcol"):
                return pair[1], pair[0]
        if isinstance(declared_order, Iterable) and not isinstance(
            declared_order, (str, bytes)
        ):
            axes = [str(item).strip().lower() for item in declared_order]
            if axes and axes[0].startswith(("y", "north", "row", "lat")):
                return pair[1], pair[0]
        return pair

    x, field_x = _lookup(raw, "origin_x", "x_origin", "easting_origin")
    y, field_y = _lookup(raw, "origin_y", "y_origin", "northing_origin")
    if x is not None or y is not None:
        if x is None or y is None:
            raise invalid_input("origin_x and origin_y must be provided together", location)
        candidates.append(
            (
                f"{field_x}/{field_y}",
                (
                    _number(x, str(field_x), location),
                    _number(y, str(field_y), location),
                ),
            )
        )

    for name in (
        "geo_origin",
        "geographic_origin",
        "origin",
        "origin_point",
        "block_origin",
    ):
        value, field = _lookup(raw, name)
        if value is None:
            continue
        candidates.append(
            (
                str(field),
                ordered_pair(value, str(field), order_value),
            )
        )
        if isinstance(value, Mapping):
            unit, unit_field = _lookup(value, "unit", "units", "coordinate_unit")
            if unit is not None:
                unit_hint = (str(unit_field), unit)

    for name, unit in (("origin_m", "meters"), ("origin_px", "pixels")):
        value, field = _lookup(raw, name)
        if value is not None:
            candidates.append(
                (
                    str(field),
                    ordered_pair(value, str(field), order_value),
                )
            )
            if unit_hint is None:
                unit_hint = (f"{field}.unit", unit)

    if not candidates:
        raise invalid_input("block origin is missing", location)
    first_field, first = candidates[0]
    for other_field, other in candidates[1:]:
        if not math.isclose(first[0], other[0], abs_tol=1e-12) or not math.isclose(
            first[1], other[1], abs_tol=1e-12
        ):
            raise BusinessError(
                "ORIGIN_AMBIGUOUS",
                "multiple origin fields disagree",
                [
                    {
                        **location,
                        "fields": [first_field, other_field],
                        "values": [list(first), list(other)],
                    }
                ],
            )
    return first, unit_hint, first_field


def _find_resolution(
    raw: Mapping[str, Any], location: dict[str, Any]
) -> tuple[float, float]:
    value = None
    field = None
    for name in (
        "resolution",
        "resolution_m",
        "pixel_size",
        "pixel_size_m",
        "pixel_resolution",
        "geo_resolution",
        "res",
    ):
        value, field = _lookup(raw, name)
        if value is not None:
            break
    if value is None:
        raise BusinessError(
            "MISSING_RESOLUTION",
            "pixel resolution is required to normalize geographic coordinates",
            [location],
        )
    resolution = _pair_from_value(value, str(field), location)
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise invalid_input("pixel resolution must be positive", location)
    resolution_unit, unit_field = _lookup(
        raw, "resolution_unit", "pixel_size_unit", "res_unit"
    )
    if resolution_unit is not None and _normalize_unit(resolution_unit) != "meters":
        raise BusinessError(
            "RESOLUTION_UNIT_UNSUPPORTED",
            "resolution must be expressed in meters per pixel",
            [
                {
                    **location,
                    "field": unit_field,
                    "value": resolution_unit,
                }
            ],
        )
    return resolution


def _find_unit(
    raw: Mapping[str, Any],
    unit_hint: tuple[str, Any] | None,
    default_unit: Any,
    location: dict[str, Any],
) -> str:
    declarations: list[tuple[str, Any]] = []
    if unit_hint is not None:
        declarations.append(unit_hint)
    for field in (
        "coordinate_unit",
        "coord_unit",
        "origin_unit",
        "geo_unit",
        "origin_units",
        "units",
        "unit",
    ):
        value, name = _lookup(raw, field)
        if value is not None:
            declarations.append((str(name), value))
    if not declarations and default_unit is not None:
        declarations.append(("request.coordinate_unit", default_unit))
    if not declarations:
        raise BusinessError(
            "COORD_UNIT_AMBIGUOUS",
            "origin coordinate unit is not declared",
            [{**location, "field": "origin_unit"}],
        )
    normalized = [(field, _normalize_unit(value), value) for field, value in declarations]
    unknown = [item for item in normalized if item[1] == "unknown"]
    if unknown:
        raise BusinessError(
            "COORD_UNIT_UNSUPPORTED",
            "origin coordinate unit must be meters or pixels",
            [
                {
                    **location,
                    "field": field,
                    "value": value,
                }
                for field, _, value in unknown
            ],
        )
    units = {item[1] for item in normalized}
    if len(units) != 1:
        raise BusinessError(
            "COORD_UNIT_AMBIGUOUS",
            "origin coordinate unit declarations disagree",
            [
                {
                    **location,
                    "fields": [field for field, _, _ in normalized],
                    "values": [value for _, _, value in normalized],
                }
            ],
        )
    return next(iter(units))


def _find_tensor(
    raw: Mapping[str, Any], location: dict[str, Any]
) -> np.ndarray:
    value = None
    for name in (
        "features",
        "feature_tensor",
        "feature_map",
        "tensor",
        "data",
        "values",
        "array",
    ):
        value, _ = _lookup(raw, name)
        if value is not None:
            break
    if value is None:
        encoded, _ = _lookup(raw, "data_b64", "tensor_b64", "features_b64")
        if encoded is None:
            raise invalid_input("feature tensor is missing", location)
        try:
            decoded = base64.b64decode(encoded)
            array = np.frombuffer(decoded, dtype=np.float64)
        except Exception:
            raise invalid_input("base64 feature tensor cannot be decoded", location) from None
        shape, _ = _lookup(raw, "shape", "tensor_shape")
        if not isinstance(shape, Iterable) or isinstance(shape, (str, bytes)):
            raise invalid_input("base64 feature tensor needs shape", location)
        try:
            array = array.reshape(tuple(int(item) for item in shape))
        except Exception:
            raise invalid_input("base64 feature tensor shape is invalid", location) from None
        return array.astype(np.float64, copy=False)

    if isinstance(value, Mapping):
        nested, _ = _lookup(value, "values", "data", "array", "tensor")
        if nested is not None:
            value = nested
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        raise invalid_input("feature tensor must be numeric", location) from None
    if array.ndim == 2:
        array = array[:, :, None]
    if array.ndim != 3:
        raise invalid_input("feature tensor must have shape [height, width, channels]", location)
    shape_value, shape_field = _lookup(raw, "shape", "tensor_shape")
    if shape_value is not None:
        try:
            declared = tuple(int(item) for item in shape_value)
        except (TypeError, ValueError):
            raise invalid_input("tensor shape must be three integers", location) from None
        if declared != tuple(array.shape):
            raise BusinessError(
                "FEATURE_SHAPE_MISMATCH",
                "declared tensor shape does not match feature data",
                [
                    {
                        **location,
                        "field": shape_field,
                        "declared": list(declared),
                        "actual": list(array.shape),
                    }
                ],
            )
    if not np.all(np.isfinite(array)):
        raise invalid_input("feature tensor contains non-finite values", location)
    return array


def _range_indices(
    value: Any, path: str, location: dict[str, Any], default_inclusive: bool = False
) -> tuple[list[int], str]:
    if isinstance(value, Mapping):
        start, _ = _lookup(value, "start", "begin", "from")
        end, _ = _lookup(value, "end", "stop", "to")
        count, _ = _lookup(value, "count", "length", "size")
        inclusive, _ = _lookup(
            value, "inclusive_end", "end_inclusive", "closed", "include_end"
        )
        if start is None:
            raise invalid_input(f"{path} range start is missing", location)
        start_int = _integer(start, f"{path}.start", location)
        if end is None and count is None:
            raise invalid_input(f"{path} range end/count is missing", location)
        inclusive_bool = default_inclusive if inclusive is None else bool(inclusive)
        if count is not None:
            end_int = start_int + _integer(count, f"{path}.count", location)
            inclusive_bool = False
        else:
            end_int = _integer(end, f"{path}.end", location)
        if inclusive_bool:
            end_int += 1
        return list(range(start_int, end_int)), "inclusive_range" if inclusive_bool else "range"

    if isinstance(value, str):
        text = value.strip()
        match = re.fullmatch(r"(\d+)\s*(?::|\.\.|~)\s*(\d+)", text)
        if match:
            return list(range(int(match.group(1)), int(match.group(2)))), "range"
        match = re.fullmatch(r"(\d+)\s*[-]\s*(\d+)", text)
        if match:
            return (
                list(range(int(match.group(1)), int(match.group(2)) + 1)),
                "inclusive_range",
            )
        pieces = [piece for piece in re.split(r"[,\s]+", text) if piece]
        if pieces:
            return [
                _integer(piece, path, location) for piece in pieces
            ], "indices"

    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        values = list(value)
        if len(values) == 2 and all(
            isinstance(item, (int, float, np.integer, np.floating)) for item in values
        ):
            start_int = _integer(values[0], f"{path}[0]", location)
            end_int = _integer(values[1], f"{path}[1]", location)
            if default_inclusive:
                end_int += 1
            return list(range(start_int, end_int)), "range"
        return [_integer(item, path, location) for item in values], "indices"

    raise invalid_input(f"{path} must be a range or channel index list", location)


def _group_selectors(
    raw_groups: Any, container_field: str, location: dict[str, Any]
) -> list[dict[str, Any]]:
    group_hint = "range" in _compact_key(container_field)
    if isinstance(raw_groups, Mapping):
        selector_fields = {
            "channels",
            "indices",
            "channel_indices",
            "channel_range",
            "range",
            "span",
            "channel_span",
            "start",
            "end",
        }
        compact_keys = {_compact_key(key) for key in raw_groups.keys()}
        if compact_keys & {_compact_key(item) for item in selector_fields}:
            return [dict(raw_groups)]
        return [
            {
                "name": str(name),
                "channels": selector,
                "_container": container_field,
            }
            for name, selector in raw_groups.items()
        ]
    if isinstance(raw_groups, Iterable) and not isinstance(raw_groups, (str, bytes)):
        result = []
        for index, selector in enumerate(raw_groups):
            if isinstance(selector, Mapping):
                item = dict(selector)
                item.setdefault("_container", container_field)
                result.append(item)
            else:
                result.append(
                    {
                        "name": f"group_{index}",
                        "channels": selector,
                        "_container": container_field,
                        "_range_hint": group_hint,
                    }
                )
        return result
    raise invalid_input("channel groups must be an object or list", location)


def _extract_channel_groups(
    raw: Mapping[str, Any], channel_count: int, location: dict[str, Any]
) -> list[dict[str, Any]]:
    raw_groups = None
    container_field = None
    for field in (
        "channel_groups",
        "channel_ranges",
        "channel_partitions",
        "channel_layout",
        "groups",
        "bands",
    ):
        raw_groups, container_field = _lookup(raw, field)
        if raw_groups is not None:
            break
    if raw_groups is None:
        raise BusinessError(
            "CHANNEL_GROUPS_MISSING",
            "channel group declarations are missing",
            [location],
        )

    groups = _group_selectors(raw_groups, str(container_field), location)
    parsed: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        name_value, name_field = _lookup(group, "name", "id", "band", "band_id", "group")
        name = str(name_value) if name_value is not None else f"group_{index}"
        selector = None
        selector_field = None
        for field in (
            "channel_indices",
            "indices",
            "channels",
            "channel_range",
            "range",
            "span",
            "channel_span",
        ):
            selector, selector_field = _lookup(group, field)
            if selector is not None:
                break
        if selector is None:
            start, _ = _lookup(group, "start", "begin", "from")
            end, _ = _lookup(group, "end", "stop", "to")
            if start is not None or end is not None:
                selector = {
                    "start": start,
                    "end": end,
                    "inclusive_end": _lookup(group, "inclusive_end")[0],
                }
                selector_field = "start/end"
        if selector is None:
            raise invalid_input(
                f"channel group {name} has no channel selector", location
            )
        range_hint = bool(group.get("_range_hint"))
        if range_hint:
            range_hint = True
        if selector_field is not None and "range" in _compact_key(str(selector_field)):
            range_hint = True
        if isinstance(selector, Mapping):
            range_hint = True
        indices, selector_kind = _range_indices(
            selector, str(selector_field), location, default_inclusive=False
        )
        if not indices:
            raise invalid_input(f"channel group {name} is empty", location)
        if len(set(indices)) != len(indices):
            raise BusinessError(
                "CHANNEL_GROUP_DUPLICATE",
                "channel group contains duplicate channel indices",
                [{**location, "group": name, "channels": indices}],
            )
        invalid_channels = [
            channel for channel in indices if channel < 0 or channel >= channel_count
        ]
        if invalid_channels:
            raise BusinessError(
                "CHANNEL_OUT_OF_RANGE",
                "channel group references channels outside the feature tensor",
                [
                    {
                        **location,
                        "group": name,
                        "channels": invalid_channels,
                        "channel_count": channel_count,
                    }
                ],
            )
        parsed.append(
            {
                "raw": dict(group),
                "name": name,
                "channels": sorted(set(indices)),
                "declared_order": list(indices),
                "selector_kind": selector_kind,
                "container_field": container_field,
                "selector_field": selector_field,
                "range_hint": range_hint,
            }
        )

    by_channel: dict[int, list[str]] = {}
    for group in parsed:
        for channel in group["channels"]:
            by_channel.setdefault(channel, []).append(group["name"])
    overlaps = {
        channel: names for channel, names in by_channel.items() if len(names) > 1
    }
    if overlaps:
        raise BusinessError(
            "CHANNEL_GROUP_OVERLAP",
            "channel groups overlap and make channel semantics ambiguous",
            [
                {
                    **location,
                    "groups": sorted(set(names)),
                    "channel": channel,
                    "channel_groups": overlaps,
                }
                for channel, names in sorted(overlaps.items())
            ],
        )
    unassigned = [
        channel for channel in range(channel_count) if channel not in by_channel
    ]
    if unassigned:
        raise BusinessError(
            "UNASSIGNED_CHANNEL",
            "every feature channel must belong to exactly one channel group",
            [{**location, "channels": unassigned, "channel_count": channel_count}],
        )
    for group in parsed:
        if len(group["channels"]) % 2 != 0:
            raise BusinessError(
                "UNPAIRED_CHANNEL",
                "RoPE requires an even number of channels in each channel group",
                [
                    {
                        **location,
                        "group": group["name"],
                        "channels": group["channels"],
                    }
                ],
            )
    return parsed


def _frequency_sources(raw: Mapping[str, Any], group_name: str) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = []
    for field in (
        "frequency_parameters",
        "frequency_params",
        "freq_params",
        "rope_parameters",
        "rope_params",
        "frequency_config",
        "frequency_bands",
        "band_parameters",
        "band_frequency_parameters",
    ):
        value, _ = _lookup(raw, field)
        if isinstance(value, Mapping):
            direct, _ = _lookup(
                value, group_name, "default", "defaults", "all", "global"
            )
            if isinstance(direct, Mapping):
                sources.append(direct)
            elif any(
                _compact_key(str(key))
                in {
                    "anglebase",
                    "thetabase",
                    "ropebase",
                    "base",
                    "frequency",
                    "freq",
                    "frequencyscale",
                    "freqscale",
                    "axis",
                    "frequencyaxis",
                    "pairfrequencies",
                    "frequencies",
                }
                for key in value.keys()
            ):
                sources.append(value)
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                item_name, _ = _lookup(item, "name", "id", "band", "band_id", "group")
                if item_name is not None and str(item_name) == group_name:
                    sources.append(item)
    return sources


def _parse_axis(value: Any, path: str, location: dict[str, Any]) -> tuple[float, float]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"x", "east", "horizontal"}:
            return 1.0, 0.0
        if normalized in {"y", "north", "vertical"}:
            return 0.0, 1.0
        if normalized in {"diagonal", "xy", "both"}:
            return 1.0, 1.0
        try:
            angle = float(normalized)
        except ValueError:
            raise invalid_input(f"{path} is not a valid frequency axis", location) from None
        return math.cos(angle), math.sin(angle)
    if isinstance(value, Mapping):
        x, _ = _lookup(value, "x", "east", "fx")
        y, _ = _lookup(value, "y", "north", "fy")
        if x is None or y is None:
            raise invalid_input(f"{path} needs x and y", location)
        return (
            _number(x, f"{path}.x", location),
            _number(y, f"{path}.y", location),
        )
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        values = list(value)
        if len(values) >= 2:
            return (
                _number(values[0], f"{path}[0]", location),
                _number(values[1], f"{path}[1]", location),
            )
    number = _number(value, path, location)
    return math.cos(number), math.sin(number)


def _parse_pair_frequencies(
    value: Any, pair_count: int, channel_count: int, path: str, location: dict[str, Any]
) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        entries = []
        for pair_id in range(pair_count):
            entry, _ = _lookup(value, str(pair_id), f"pair_{pair_id}", f"p{pair_id}")
            if entry is not None:
                entries.append(entry)
        if len(entries) != pair_count:
            raise invalid_input(f"{path} must define one value per channel pair", location)
        value = entries
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise invalid_input(f"{path} must be a list", location)
    values = list(value)
    if len(values) == channel_count:
        values = [values[index] for index in range(0, channel_count, 2)]
    if len(values) != pair_count:
        raise invalid_input(f"{path} length must equal channel pair count", location)
    vectors: list[tuple[float, float]] = []
    for index, entry in enumerate(values):
        if isinstance(entry, Mapping) or (
            isinstance(entry, Iterable) and not isinstance(entry, (str, bytes))
        ):
            vector = _pair_from_value(entry, f"{path}[{index}]", location)
        else:
            vector = (_number(entry, f"{path}[{index}]", location), 0.0)
        vectors.append(vector)
    return np.asarray(vectors, dtype=np.float64)


def _frequency_parameter(
    block_raw: Mapping[str, Any],
    group_raw: Mapping[str, Any],
    group_name: str,
    pair_count: int,
    channel_count: int,
    location: dict[str, Any],
) -> tuple[FrequencyBand, dict[str, Any]]:
    layers: list[tuple[str, Mapping[str, Any]]] = [
        ("group", group_raw),
    ]
    nested_names = (
        "frequency",
        "freq",
        "rope",
        "rope_config",
        "frequency_config",
        "rope_parameters",
    )
    for field in nested_names:
        value, _ = _lookup(group_raw, field)
        if isinstance(value, Mapping):
            layers.append((f"group.{field}", value))
    for source in _frequency_sources(block_raw, group_name):
        layers.append(("block.frequency_parameters", source))
    block_nested = None
    for field in ("rope", "frequency", "frequency_config", "freq"):
        value, _ = _lookup(block_raw, field)
        if isinstance(value, Mapping):
            block_nested = value
            break
    if block_nested is not None:
        layers.append(("block.rope", block_nested))
    layers.append(("block", block_raw))

    angle_base = None
    frequency_scale = None
    axis = None
    frequencies = None
    used_fields: dict[str, str] = {}
    for layer_name, layer in layers:
        if angle_base is None:
            value, field = _lookup(
                layer,
                "angle_base",
                "theta_base",
                "rope_base",
                "base_theta",
                "base",
                "theta",
            )
            if value is not None:
                angle_base = _number(value, str(field), location)
                used_fields["angle_base"] = f"{layer_name}.{field}"
        if frequency_scale is None:
            value, field = _lookup(
                layer,
                "frequency_scale",
                "freq_scale",
                "scale",
                "frequency_scale_m",
            )
            if value is not None:
                frequency_scale = _number(value, str(field), location)
                used_fields["frequency_scale"] = f"{layer_name}.{field}"
        if axis is None:
            value, field = _lookup(
                layer,
                "frequency_axis",
                "axis",
                "direction",
                "orientation",
                "geo_axis",
            )
            if value is not None:
                axis = _parse_axis(value, str(field), location)
                used_fields["axis"] = f"{layer_name}.{field}"
        if frequencies is None:
            value, field = _lookup(
                layer,
                "pair_frequencies",
                "frequencies",
                "frequency_values",
                "freq_values",
                "frequency_vector",
            )
            if value is not None:
                frequencies = _parse_pair_frequencies(
                    value,
                    pair_count,
                    channel_count,
                    str(field),
                    location,
                )
                used_fields["pair_frequencies"] = f"{layer_name}.{field}"

    for layer_name, layer in (("group", group_raw), ("block", block_raw)):
        value, field = _lookup(layer, "frequency", "freq")
        if value is None or isinstance(value, Mapping):
            continue
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            if frequencies is None:
                frequencies = _parse_pair_frequencies(
                    value, pair_count, channel_count, str(field), location
                )
                used_fields["pair_frequencies"] = f"{layer_name}.{field}"
        elif frequency_scale is None:
            frequency_scale = _number(value, str(field), location)
            used_fields["frequency_scale"] = f"{layer_name}.{field}"

    if angle_base is None:
        angle_base = 10000.0
        used_fields["angle_base"] = "default(10000)"
    if frequency_scale is None:
        frequency_scale = 1.0
        used_fields["frequency_scale"] = "default(1)"
    if axis is None:
        axis = (1.0, 0.0)
        used_fields["axis"] = "default(x-axis)"
    if angle_base <= 0:
        raise invalid_input("angle base must be positive", location)
    if not math.isfinite(frequency_scale):
        raise invalid_input("frequency scale must be finite", location)
    if not all(math.isfinite(item) for item in axis):
        raise invalid_input("frequency axis must be finite", location)
    if frequencies is not None and frequencies.shape != (pair_count, 2):
        raise invalid_input("pair frequencies have an invalid shape", location)

    return (
        FrequencyBand(
            name=group_name,
            channels=tuple(int(item) for item in group_raw["channels"]),
            angle_base=angle_base,
            frequency_scale=frequency_scale,
            axis=axis,
            pair_frequencies=frequencies,
            source={
                "fields": used_fields,
                "group_selector": {
                    "field": group_raw.get("selector_field"),
                    "container": group_raw.get("container_field"),
                    "kind": group_raw.get("selector_kind"),
                    "declared_channels": group_raw.get("declared_order"),
                },
            },
        ),
        used_fields,
    )


def _band_combinations(
    payload: Mapping[str, Any],
    blocks: list[NormalizedBlock],
    location: dict[str, Any],
) -> list[BandCombination]:
    raw_combinations = None
    for field in (
        "band_combinations",
        "frequency_combinations",
        "combinations",
        "band_combination",
    ):
        raw_combinations, _ = _lookup(payload, field)
        if raw_combinations is not None:
            break
    band_names = sorted({band.name for block in blocks for band in block.bands})
    if raw_combinations is None:
        return [
            BandCombination(
                name="all",
                band_names=tuple(band_names),
                source={"field": "default", "value": "all declared bands"},
            )
        ]
    if isinstance(raw_combinations, Mapping):
        raw_combinations = [
            {"name": str(name), **dict(value)}
            if isinstance(value, Mapping)
            else {"name": str(name), "bands": value}
            for name, value in raw_combinations.items()
        ]
    if not isinstance(raw_combinations, Iterable) or isinstance(
        raw_combinations, (str, bytes)
    ):
        raise invalid_input("band combinations must be a list", location)

    combinations: list[BandCombination] = []
    for index, raw in enumerate(raw_combinations):
        if isinstance(raw, str):
            raw = {"name": raw, "bands": [raw]}
        if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, Mapping)):
            raw = {"name": f"combination_{index}", "bands": list(raw)}
        if not isinstance(raw, Mapping):
            raise invalid_input("band combination entry must be an object", location)
        name_value, _ = _lookup(raw, "name", "id", "combination", "combo")
        name = str(name_value) if name_value is not None else f"combination_{index}"
        bands_value, bands_field = _lookup(
            raw,
            "bands",
            "band_ids",
            "band_names",
            "groups",
            "frequency_parameters",
        )
        if bands_value is None:
            bands_value = []
        if isinstance(bands_value, str):
            selected_names = [bands_value]
        elif isinstance(bands_value, Iterable) and not isinstance(
            bands_value, (bytes, Mapping)
        ):
            selected_names = [str(item) for item in bands_value]
        else:
            raise invalid_input(f"{bands_field} must be a list", location)
        if not selected_names:
            selected_names = band_names
        duplicates = sorted(
            name
            for name in set(selected_names)
            if selected_names.count(name) > 1
        )
        if duplicates:
            raise BusinessError(
                "BAND_COMBINATION_OVERLAP",
                "a band appears more than once in one frequency combination",
                [
                    {
                        **location,
                        "combination": name,
                        "bands": duplicates,
                    }
                ],
            )
        unknown = sorted(set(selected_names) - set(band_names))
        if unknown:
            raise BusinessError(
                "BAND_COMBINATION_UNKNOWN",
                "frequency combination references an unknown band",
                [
                    {
                        **location,
                        "combination": name,
                        "bands": unknown,
                        "known_bands": band_names,
                    }
                ],
            )
        channel_value, channel_field = _lookup(
            raw, "channels", "channel_indices", "channel_filter"
        )
        channel_filter = None
        if channel_value is not None:
            indices, _ = _range_indices(
                channel_value, str(channel_field), location, default_inclusive=False
            )
            if not indices:
                raise invalid_input("combination channel filter is empty", location)
            channel_filter = tuple(sorted(set(indices)))
        combinations.append(
            BandCombination(
                name=name,
                band_names=tuple(selected_names),
                channel_filter=channel_filter,
                source={
                    "bands_field": bands_field,
                    "channel_field": channel_field,
                },
            )
        )
    duplicate_names = sorted(
        name
        for name in {item.name for item in combinations}
        if sum(item.name == name for item in combinations) > 1
    )
    if duplicate_names:
        raise BusinessError(
            "BAND_COMBINATION_DUPLICATE",
            "frequency combination names must be unique",
            [{**location, "combinations": duplicate_names}],
        )
    return combinations


def _validate_combination_pairs(
    request: NormalizedRequest,
) -> None:
    for combination in request.combinations:
        for block in request.blocks:
            block_bands = {band.name: band for band in block.bands}
            missing = [name for name in combination.band_names if name not in block_bands]
            if missing:
                raise BusinessError(
                    "BAND_COMBINATION_UNKNOWN",
                    "frequency combination is not declared by this block",
                    [
                        {
                            "block_id": block.block_id,
                            "combination": combination.name,
                            "bands": missing,
                        }
                    ],
                )
            active_channels: set[int] = set()
            for band_name in combination.band_names:
                band = block_bands[band_name]
                active_channels.update(band.channels)
            if combination.channel_filter is not None:
                invalid = [
                    channel
                    for channel in combination.channel_filter
                    if channel < 0 or channel >= block.channels
                ]
                if invalid:
                    raise BusinessError(
                        "CHANNEL_OUT_OF_RANGE",
                        "combination channel filter is out of range",
                        [
                            {
                                "block_id": block.block_id,
                                "combination": combination.name,
                                "channels": invalid,
                            }
                        ],
                    )
                active_channels &= set(combination.channel_filter)
            if not active_channels:
                raise invalid_input(
                    "frequency combination has no active channels",
                    {"block_id": block.block_id, "combination": combination.name},
                )
            selected_per_band: dict[str, list[int]] = {}
            for band_name in combination.band_names:
                band = block_bands[band_name]
                selected = [
                    channel for channel in band.channels if channel in active_channels
                ]
                if selected:
                    selected_per_band[band_name] = selected
            for band_name, selected in selected_per_band.items():
                band = block_bands[band_name]
                if len(selected) % 2 != 0:
                    raise BusinessError(
                        "UNPAIRED_CHANNEL",
                        "combination channel filter splits a RoPE channel pair",
                        [
                            {
                                "block_id": block.block_id,
                                "combination": combination.name,
                                "band": band_name,
                                "channels": selected,
                            }
                        ],
                    )
                for pair_id in range(band.pair_count):
                    pair = set(band.channels[pair_id * 2 : pair_id * 2 + 2])
                    if pair & active_channels and not pair <= active_channels:
                        raise BusinessError(
                            "UNPAIRED_CHANNEL",
                            "combination channel filter splits a RoPE channel pair",
                            [
                                {
                                    "block_id": block.block_id,
                                    "combination": combination.name,
                                    "band": band_name,
                                    "channels": sorted(pair),
                                }
                            ],
                        )


def _validate_block_extents(request: NormalizedRequest) -> None:
    location = {"request_id": request.request_id}
    keys = sorted(
        {(block.schema_version, block.tile_set_id) for block in request.blocks}
    )
    for version, tile_set in keys:
        blocks = request.blocks_for_set(version, tile_set)
        for left_index, left in enumerate(blocks):
            left_extent = left.extent_m()
            for right in blocks[left_index + 1 :]:
                right_extent = right.extent_m()
                overlap_x = min(left_extent[1], right_extent[1]) - max(
                    left_extent[0], right_extent[0]
                )
                overlap_y = min(left_extent[3], right_extent[3]) - max(
                    left_extent[2], right_extent[2]
                )
                if overlap_x > request.tolerance and overlap_y > request.tolerance:
                    raise BusinessError(
                        "BLOCK_EXTENT_CONFLICT",
                        "feature blocks overlap in geographic space",
                        [
                            {
                                **location,
                                "schema_version": version,
                                "tile_set_id": tile_set,
                                "blocks": [left.block_id, right.block_id],
                                "extents_m": [list(left_extent), list(right_extent)],
                                "overlap_m": [overlap_x, overlap_y],
                            }
                        ],
                    )


def normalize_request(payload: Mapping[str, Any]) -> NormalizedRequest:
    if not isinstance(payload, Mapping):
        raise invalid_input("request body must be a JSON object")
    tolerance_value, _ = _lookup(payload, "tolerance", "angle_tolerance", "atol")
    tolerance = 1e-6 if tolerance_value is None else _number(
        tolerance_value, "tolerance", {}
    )
    threshold_value, _ = _lookup(
        payload, "phase_threshold", "continuity_threshold", "seam_threshold"
    )
    phase_threshold = 1e-6 if threshold_value is None else _number(
        threshold_value, "phase_threshold", {}
    )
    if tolerance < 0 or phase_threshold < 0:
        raise invalid_input("tolerances must be non-negative")
    request_id_value, _ = _lookup(payload, "request_id", "id", "run_id")
    request_id = str(request_id_value) if request_id_value is not None else "request"
    pixel_anchor_value, anchor_field = _lookup(
        payload, "pixel_anchor", "origin_reference", "position_anchor"
    )
    default_anchor = _normalize_anchor(pixel_anchor_value)
    default_unit, unit_field = _lookup(
        payload,
        "coordinate_unit",
        "coord_unit",
        "origin_unit",
        "geo_unit",
        "unit",
    )

    raw_blocks = _block_containers(payload)
    normalized_blocks: list[NormalizedBlock] = []
    used_block_ids: set[str] = set()
    for index, raw in enumerate(raw_blocks):
        version_value, version_field = _lookup(
            raw, "schema_version", "schema", "version", "model_version"
        )
        if version_value is None:
            raise BusinessError(
                "SCHEMA_VERSION_MISSING",
                "each feature block must declare its schema version",
                [{"block_index": index, "field": "schema_version"}],
            )
        schema_version = _normalize_schema_version(version_value)
        if schema_version not in {"v1", "v2", "v3"}:
            raise BusinessError(
                "SCHEMA_VERSION_UNSUPPORTED",
                "schema version must be v1, v2 or v3",
                [
                    {
                        "block_index": index,
                        "field": version_field,
                        "value": version_value,
                    }
                ],
            )
        block_id_value, _ = _lookup(raw, "block_id", "id", "chunk_id", "feature_id")
        block_id = (
            str(block_id_value)
            if block_id_value is not None
            else f"{schema_version}-block-{index}"
        )
        if block_id in used_block_ids:
            raise BusinessError(
                "BLOCK_ID_DUPLICATE",
                "feature block identifiers must be unique",
                [{"block_id": block_id}],
            )
        used_block_ids.add(block_id)
        location = {"block_id": block_id, "schema_version": schema_version}

        tile_set_value, tile_set_field = _lookup(
            raw, "tile_set_id", "tiling_id", "tile_set", "scene_id", "scene", "region_id"
        )
        tile_set_id = (
            str(tile_set_value)
            if tile_set_value is not None
            else f"{schema_version}:default"
        )
        tile_key_value, tile_key_field = _lookup(
            raw, "tile_key", "canonical_tile_id", "grid_key", "tile_uid", "tile_id"
        )
        tile_key = str(tile_key_value) if tile_key_value is not None else block_id

        anchor_value, anchor_field = _lookup(
            raw, "pixel_anchor", "origin_reference", "position_anchor"
        )
        if anchor_value is None:
            if (
                default_anchor == "center"
                and pixel_anchor_value is None
                and anchor_field is None
            ):
                pixel_anchor = "center"
            else:
                pixel_anchor = default_anchor
        else:
            pixel_anchor = _normalize_anchor(anchor_value)
        if pixel_anchor not in {"center", "corner"}:
            raise invalid_input("pixel anchor must be center or corner", location)

        origin, unit_hint, origin_field = _find_origin(raw, location)
        resolution = _find_resolution(raw, location)
        origin_unit = _find_unit(raw, unit_hint, default_unit, location)
        if origin_unit == "pixels":
            origin_m = (
                origin[0] * resolution[0],
                origin[1] * resolution[1],
            )
        else:
            origin_m = origin

        local_offset_value, offset_field = _lookup(
            raw,
            "local_index_origin",
            "local_origin",
            "local_index_offset",
            "spatial_index_origin",
        )
        if local_offset_value is not None:
            row_offset, col_offset = (
                _integer(item, str(offset_field), location)
                for item in _pair_from_value(local_offset_value, str(offset_field), location)
            )
        else:
            row_value, row_field = _lookup(raw, "row_offset", "local_row_offset")
            col_value, col_field = _lookup(raw, "col_offset", "local_col_offset")
            row_offset = (
                _integer(row_value, str(row_field), location) if row_value is not None else 0
            )
            col_offset = (
                _integer(col_value, str(col_field), location) if col_value is not None else 0
            )

        data = _find_tensor(raw, location)
        channel_count = data.shape[2]
        raw_groups = _extract_channel_groups(raw, channel_count, location)
        bands: list[FrequencyBand] = []
        frequency_fields: dict[str, Any] = {}
        for group in raw_groups:
            merged_group = dict(group["raw"])
            merged_group.update(
                {
                    "channels": group["channels"],
                    "declared_order": group["declared_order"],
                    "selector_kind": group["selector_kind"],
                    "container_field": group["container_field"],
                    "selector_field": group["selector_field"],
                }
            )
            band, fields = _frequency_parameter(
                raw,
                merged_group,
                group["name"],
                len(group["channels"]) // 2,
                len(group["channels"]),
                location,
            )
            bands.append(band)
            frequency_fields[group["name"]] = fields
        normalized_blocks.append(
            NormalizedBlock(
                block_id=block_id,
                schema_version=schema_version,
                tile_set_id=tile_set_id,
                tile_key=tile_key,
                data=data,
                local_row_offset=row_offset,
                local_col_offset=col_offset,
                origin_m=origin_m,
                resolution_m=resolution,
                raw_origin=origin,
                origin_unit=origin_unit,
                pixel_anchor=pixel_anchor,
                bands=bands,
                block_source={
                    "schema_version_field": version_field,
                    "origin_field": origin_field,
                    "origin_unit_field": unit_field,
                    "tile_set_field": tile_set_field,
                    "tile_key_field": tile_key_field,
                    "local_offset_field": offset_field,
                    "group_container_field": raw_groups[0]["container_field"],
                    "group_selector_kinds": {
                        group["name"]: group["selector_kind"] for group in raw_groups
                    },
                    "frequency_fields": frequency_fields,
                    "pixel_anchor_field": anchor_field,
                },
            )
        )

    channel_counts = {block.channels for block in normalized_blocks}
    if len(channel_counts) != 1:
        raise BusinessError(
            "CHANNEL_COUNT_MISMATCH",
            "all schema blocks must describe the same feature channel count",
            [
                {
                    "block_id": block.block_id,
                    "schema_version": block.schema_version,
                    "channel_count": block.channels,
                }
                for block in normalized_blocks
            ],
        )

    request = NormalizedRequest(
        request_id=request_id,
        blocks=normalized_blocks,
        combinations=[],
        tolerance=tolerance,
        phase_threshold=phase_threshold,
        pixel_anchor=default_anchor,
        metadata={
            "schema_version_field": "schema_version",
            "coordinate_unit_field": unit_field,
            "pixel_anchor_field": anchor_field,
        },
    )
    request.combinations = _band_combinations(
        payload, normalized_blocks, {"request_id": request_id}
    )
    _validate_combination_pairs(request)
    _validate_block_extents(request)
    return request
