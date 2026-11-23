"""Configuration normalization and validation."""

from __future__ import annotations

import copy
from typing import Any

import numpy as np

from .errors import BusinessError, ErrorCode
from .graph import GraphSpec
from .hashing import sha256_json, validate_finite_number

PRECISION_ALIASES = {
    "high": "high",
    "float64": "high",
    "f64": "high",
    "fp64": "high",
    "double": "high",
    "standard": "standard",
    "float32": "standard",
    "f32": "standard",
    "fp32": "standard",
    "single": "standard",
}

PRECISION_DTYPES = {
    "high": np.float64,
    "standard": np.float32,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "context_radius": 6,
    "style": {
        "style_strength": 0.42,
        "color_matrix": [
            [1.06, 0.02, -0.02],
            [0.01, 0.97, 0.04],
            [-0.02, 0.03, 1.07],
        ],
        "edge_preserve": 0.62,
        "iterations": 8,
        "precision": "high",
        "blend": {"feather_px": 5},
    },
    "limits": {
        "max_regions": 64,
        "max_resident_pixels": 150_000_000,
        "max_parallel_regions": 4,
        "resident_node_copies": 6,
    },
    "graph": {
        "nodes": [
            "mask_parse",
            "boundary_context",
            "content_completion",
            "style_transfer",
            "blend_output",
        ],
        "edges": [
            ["mask_parse", "boundary_context"],
            ["boundary_context", "content_completion"],
            ["content_completion", "style_transfer"],
            ["style_transfer", "blend_output"],
        ],
    },
}


def _positive_int(value: Any, field: str, code: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise BusinessError(
            code,
            f"{field} must be a positive integer",
            region_id=None,
            field=field,
            value=value,
        )
    number = int(value)
    if number <= 0:
        raise BusinessError(
            code,
            f"{field} must be positive",
            region_id=None,
            field=field,
            value=value,
        )
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            f"{field} must be a non-negative integer",
            region_id=None,
            field=field,
            value=value,
        )
    number = int(value)
    if number < 0:
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            f"{field} must be non-negative",
            region_id=None,
            field=field,
            value=value,
        )
    return number


def normalize_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize flat or nested configuration into one canonical dictionary."""

    merged = copy.deepcopy(DEFAULT_CONFIG)
    if config is not None and not isinstance(config, dict):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "configuration must be a dictionary",
            region_id=None,
            field="config",
            actual_type=type(config).__name__,
        )
    supplied = copy.deepcopy(config or {})

    if "context_radius" in supplied:
        merged["context_radius"] = supplied["context_radius"]

    style_value = supplied.get("style") or {}
    if not isinstance(style_value, dict):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "style configuration must be a dictionary",
            region_id=None,
            field="style",
            actual_type=type(style_value).__name__,
        )
    supplied_style = dict(style_value)
    for key in (
        "style_strength",
        "color_matrix",
        "edge_preserve",
        "iterations",
        "precision",
        "blend",
    ):
        if key in supplied:
            supplied_style[key] = supplied[key]
    merged["style"].update(supplied_style)

    if "feather_px" in supplied:
        merged["style"].setdefault("blend", {})["feather_px"] = supplied["feather_px"]

    limits_value = supplied.get("limits") or {}
    if not isinstance(limits_value, dict):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "limits configuration must be a dictionary",
            region_id=None,
            field="limits",
            actual_type=type(limits_value).__name__,
        )
    merged["limits"].update(dict(limits_value))
    if supplied.get("graph") is not None:
        if not isinstance(supplied["graph"], dict):
            raise BusinessError(
                ErrorCode.INVALID_GRAPH,
                "graph configuration must be a dictionary",
                field="graph",
                actual_type=type(supplied["graph"]).__name__,
            )
        merged["graph"] = copy.deepcopy(supplied["graph"])

    context_radius = _positive_int(
        merged.get("context_radius"),
        "context_radius",
        ErrorCode.INVALID_CONTEXT_RADIUS,
    )

    style = merged["style"]
    strength = validate_finite_number(
        style.get("style_strength"), "style.style_strength", ErrorCode.STYLE_STRENGTH_RANGE
    )
    if not 0.0 <= strength <= 1.0:
        raise BusinessError(
            ErrorCode.STYLE_STRENGTH_RANGE,
            "style strength must be in [0, 1]",
            region_id=None,
            field="style.style_strength",
            value=style.get("style_strength"),
            min=0.0,
            max=1.0,
        )

    edge_preserve = validate_finite_number(
        style.get("edge_preserve"), "style.edge_preserve"
    )
    if not 0.0 <= edge_preserve <= 1.0:
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "edge preservation coefficient must be in [0, 1]",
            region_id=None,
            field="style.edge_preserve",
            value=style.get("edge_preserve"),
            min=0.0,
            max=1.0,
        )

    iterations = _positive_int(
        style.get("iterations"), "style.iterations", ErrorCode.INVALID_ITERATIONS
    )
    precision_raw = style.get("precision")
    if not isinstance(precision_raw, str) or precision_raw.lower() not in PRECISION_ALIASES:
        raise BusinessError(
            ErrorCode.UNKNOWN_PRECISION,
            "precision must be one of high, standard, float64, float32, f64, f32, fp64, fp32",
            region_id=None,
            field="style.precision",
            value=precision_raw,
            allowed=sorted(PRECISION_ALIASES),
        )
    precision = PRECISION_ALIASES[precision_raw.lower()]

    matrix = np.asarray(style.get("color_matrix"), dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        raise BusinessError(
            ErrorCode.INVALID_COLOR_MATRIX,
            "color migration matrix must be a finite 3x3 matrix",
            region_id=None,
            field="style.color_matrix",
            shape=list(matrix.shape),
        )

    blend_value = style.get("blend") or {}
    if not isinstance(blend_value, dict):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "blend configuration must be a dictionary",
            region_id=None,
            field="style.blend",
            actual_type=type(blend_value).__name__,
        )
    blend = dict(blend_value)
    feather_px = _nonnegative_int(
        blend.get("feather_px", 5), "style.blend.feather_px"
    )

    limits = dict(merged.get("limits") or {})
    max_regions = _positive_int(
        limits.get("max_regions"), "limits.max_regions", ErrorCode.INVALID_CONFIG
    )
    max_resident_pixels = _positive_int(
        limits.get("max_resident_pixels"),
        "limits.max_resident_pixels",
        ErrorCode.INVALID_CONFIG,
    )
    max_parallel_regions = _positive_int(
        limits.get("max_parallel_regions"),
        "limits.max_parallel_regions",
        ErrorCode.INVALID_CONFIG,
    )
    resident_node_copies = _positive_int(
        limits.get("resident_node_copies"),
        "limits.resident_node_copies",
        ErrorCode.INVALID_CONFIG,
    )

    graph = GraphSpec.from_config(merged)

    normalized = {
        "context_radius": context_radius,
        "style": {
            "style_strength": strength,
            "color_matrix": [[float(v) for v in row] for row in matrix.tolist()],
            "edge_preserve": edge_preserve,
            "iterations": iterations,
            "precision": precision,
            "blend": {"feather_px": feather_px},
        },
        "limits": {
            "max_regions": max_regions,
            "max_resident_pixels": max_resident_pixels,
            "max_parallel_regions": max_parallel_regions,
            "resident_node_copies": resident_node_copies,
        },
        "graph": {
            "nodes": list(graph.nodes),
            "edges": [list(edge) for edge in graph.edges],
        },
    }
    return normalized


def style_snapshot_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "context_radius": config["context_radius"],
        "style": copy.deepcopy(config["style"]),
    }


def style_snapshot_checksum(config: dict[str, Any]) -> str:
    return sha256_json({"kind": "style", "payload": style_snapshot_payload(config)})


def merge_config(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    """Deep-merge a partial configuration update onto a base configuration."""

    result = copy.deepcopy(base or {})
    incoming = copy.deepcopy(override or {})

    def walk(target: dict[str, Any], source: dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                walk(target[key], value)
            else:
                target[key] = value

    walk(result, incoming)
    return result


def style_projection(config: dict[str, Any], node: str) -> dict[str, Any]:
    """Style fields semantically consumed by a node.

    The cache key always contains a ``style_param_version`` entry. Upstream
    nodes use a stable projection for fields they do not consume, which is what
    permits mask/context/inpaint reuse when only style strength changes.
    """

    from .graph import (
        NODE_BLEND_OUTPUT,
        NODE_CONTENT_COMPLETION,
        NODE_CONTEXT_SAMPLE,
        NODE_MASK_PARSE,
        NODE_STYLE_TRANSFER,
    )

    style = config["style"]
    core = {
        "style_strength": style["style_strength"],
        "color_matrix": style["color_matrix"],
        "edge_preserve": style["edge_preserve"],
        "iterations": style["iterations"],
        "precision": style["precision"],
    }
    if node == NODE_MASK_PARSE:
        return {"projection": "mask-independent", "schema": 1}
    if node == NODE_CONTEXT_SAMPLE:
        return {"projection": "context-independent", "schema": 1}
    if node == NODE_CONTENT_COMPLETION:
        return {
            "projection": "inpaint",
            "iterations": style["iterations"],
            "precision": style["precision"],
        }
    if node == NODE_STYLE_TRANSFER:
        return {"projection": "style", **core}
    if node == NODE_BLEND_OUTPUT:
        return {
            "projection": "blend",
            **core,
            "feather_px": style["blend"]["feather_px"],
        }
    raise BusinessError(
        ErrorCode.INVALID_GRAPH,
        "unknown node for style projection",
        node=node,
    )


def style_projection_digest(config: dict[str, Any], node: str) -> str:
    return sha256_json(style_projection(config, node))
