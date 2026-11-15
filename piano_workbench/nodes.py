"""Parameterized audio state-tree nodes.

Each node records the operation type, the sample-range selection, the
parameters and the parent node id. Node ids are *content-addressed*: derived
from a canonical serialization of (op, parent, selection, params), so the same
operation applied to the same parent always yields the same id anywhere.

The audio cache descriptor is derived data and intentionally excluded from the
id hash, which keeps the state tree semantics independent of storage layout.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .errors import WorkbenchError, E_PARAM, E_CACHE_HASH

OP_DENOISE = "denoise"
OP_ENVELOPE = "envelope"
OP_FADE = "fade"
OP_PATCH = "patch"
OP_ROOT = "root"

OP_TYPES = (OP_DENOISE, OP_ENVELOPE, OP_FADE, OP_PATCH)
_ALL_OPS = OP_TYPES + (OP_ROOT,)

_DEFAULT_PARAMS = {
    OP_DENOISE: {
        "over_subtraction": 2.0,
        "floor": 0.02,
        "noise_frame_count": 12,
        "seed": 20240517,
    },
    OP_ENVELOPE: {"gain_start": 1.0, "gain_end": None},
    OP_FADE: {"mode": "in", "curve": "linear"},
    OP_PATCH: {"margin": 2048, "gl_iterations": 32, "seed": 12345},
}

_REQUIRED = {
    OP_ENVELOPE: ("gain_start",),
    OP_FADE: (),
}


def canonical_json(payload: Any) -> str:
    """Stable JSON text: sorted keys, compact separators, no ASCII escaping."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_id(payload: dict) -> str:
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return "n_" + digest[:16]


def normalize_params(op_type: str, params: dict | None) -> dict:
    """Fill defaults and reject structurally invalid parameter sets."""

    if op_type not in OP_TYPES:
        raise WorkbenchError(E_PARAM, f"unknown operation type: {op_type!r}")
    merged = dict(_DEFAULT_PARAMS[op_type])
    merged.update(dict(params or {}))
    for required in _REQUIRED.get(op_type, ()):
        if required not in merged or merged[required] is None:
            raise WorkbenchError(
                E_PARAM, f"operation {op_type!r} requires parameter {required!r}"
            )
    if op_type == OP_FADE and merged["mode"] not in ("in", "out"):
        raise WorkbenchError(E_PARAM, "fade mode must be 'in' or 'out'")
    if op_type == OP_FADE and merged["curve"] not in ("linear", "equal_power"):
        raise WorkbenchError(E_PARAM, "fade curve must be 'linear' or 'equal_power'")
    return merged


def build_root_node(
    source_name: str, sample_count: int, sample_rate: int, content_hash: str
) -> dict:
    """Create the immutable root node describing the imported base clip."""

    payload = {
        "op": OP_ROOT,
        "parent_id": None,
        "selection": None,
        "params": {
            "source_name": source_name,
            "sample_count": int(sample_count),
            "sample_rate": int(sample_rate),
            "content_hash": content_hash,
        },
    }
    node = dict(payload)
    node["id"] = _content_id(payload)
    node["cache"] = None
    return node


def build_node(
    parent_id: str, op_type: str, selection: tuple[int, int], params: dict | None
) -> dict:
    """Build a content-addressed child node (not yet attached to a cache)."""

    if not isinstance(parent_id, str) or not parent_id:
        raise WorkbenchError(E_PARAM, "parent_id must be a non-empty string")
    if op_type not in OP_TYPES:
        raise WorkbenchError(E_PARAM, f"unknown operation type: {op_type!r}")
    start, end = int(selection[0]), int(selection[1])
    clean_params = normalize_params(op_type, params)
    payload = {
        "op": op_type,
        "parent_id": parent_id,
        "selection": [start, end],
        "params": clean_params,
    }
    node = dict(payload)
    node["id"] = _content_id(payload)
    node["cache"] = None
    return node


def node_to_dict(node: dict) -> dict:
    """Pure JSON-serializable view of a node."""

    return {
        "id": node["id"],
        "op": node["op"],
        "parent_id": node["parent_id"],
        "selection": list(node["selection"]) if node["selection"] is not None else None,
        "params": node["params"],
        "cache": dict(node["cache"]) if node.get("cache") else None,
    }


def node_from_dict(data: dict) -> dict:
    """Rebuild an in-memory node from persisted JSON and re-verify its id."""

    if data.get("op") not in _ALL_OPS:
        raise WorkbenchError(E_PARAM, f"persisted node has unknown op: {data.get('op')!r}")
    payload = {
        "op": data["op"],
        "parent_id": data["parent_id"],
        "selection": data["selection"],
        "params": data["params"],
    }
    node = dict(payload)
    node["id"] = _content_id(payload)
    if node["id"] != data["id"]:
        raise WorkbenchError(
            E_CACHE_HASH,
            "persisted node id does not match its content hash",
            {"stored": data["id"], "computed": node["id"]},
        )
    node["cache"] = dict(data["cache"]) if data.get("cache") else None
    return node
