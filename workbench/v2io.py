"""v2 exchange format: JSON array with float precision and metadata.

Array elements are either:
  {"kind": "color_table", "colors": {name: [r, g, b], ...}}
  {"kind": "instance", "id", "category", "bbox": [float x4],
   "confidence": float 0..1, "relations": [ref, ...], "color_name": str}
"""

from __future__ import annotations

import json

from .errors import (E_CONFIDENCE_RANGE, E_PARSE_INVALID, E_REGION_INVALID,
                     WorkbenchError)

REQUIRED_FIELDS = ("id", "category", "bbox", "confidence")


def serialize_v2(instances: list[dict], color_table: dict) -> str:
    doc = [{"kind": "color_table", "colors": color_table}] + instances
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def _validate_instance(entry: dict, index: int, bounds) -> dict:
    for f in REQUIRED_FIELDS:
        if f not in entry:
            raise WorkbenchError(E_PARSE_INVALID,
                                 f"index {index}: missing field '{f}'",
                                 location=index)
    bbox = entry["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise WorkbenchError(E_PARSE_INVALID,
                             f"index {index}: bbox must be a 4-element list",
                             location=index)
    try:
        bbox = [float(v) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise WorkbenchError(E_PARSE_INVALID,
                             f"index {index}: non-numeric bbox ({exc})",
                             location=index) from exc
    conf = entry["confidence"]
    if not isinstance(conf, (int, float)):
        raise WorkbenchError(E_PARSE_INVALID,
                             f"index {index}: confidence not numeric",
                             location=index)
    conf = float(conf)
    if not 0.0 <= conf <= 1.0:
        raise WorkbenchError(E_CONFIDENCE_RANGE,
                             f"index {index}: confidence {conf} outside [0, 1]",
                             location=index)
    w, h = bounds
    x0, y0, x1, y1 = bbox
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x0 >= x1 or y0 >= y1:
        raise WorkbenchError(E_REGION_INVALID,
                             f"bbox {bbox} outside image {w}x{h}",
                             location=index)
    relations = entry.get("relations", [])
    if not isinstance(relations, list):
        raise WorkbenchError(E_PARSE_INVALID,
                             f"index {index}: relations must be a list",
                             location=index)
    return {
        "inst_id": str(entry["id"]),
        "category": str(entry["category"]),
        "bbox": bbox,
        "confidence": conf,
        "relations": [str(r) for r in relations],
        "color_name": entry.get("color_name"),
    }


def parse_v2(text: str, bounds) -> tuple[list[dict], dict, list[dict]]:
    """Parse v2 JSON. Returns (instances, color_table, errors).

    Errors carry the array index (or null for a whole-file JSON failure).
    """
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], {}, [{"line": None, "index": None, "code": E_PARSE_INVALID,
                         "detail": f"invalid JSON: {exc}"}]
    if not isinstance(doc, list):
        return [], {}, [{"line": None, "index": None, "code": E_PARSE_INVALID,
                         "detail": "top level must be a JSON array"}]
    instances, color_table, errors = [], {}, []
    for index, entry in enumerate(doc):
        if not isinstance(entry, dict):
            errors.append({"line": None, "index": index, "code": E_PARSE_INVALID,
                           "detail": f"index {index}: element is not an object"})
            continue
        if entry.get("kind") == "color_table":
            color_table = {str(k): list(v) for k, v in entry.get("colors", {}).items()}
            continue
        try:
            instances.append(_validate_instance(entry, index, bounds))
        except WorkbenchError as exc:
            errors.append({"line": None, "index": index, "code": exc.code,
                           "detail": exc.detail})
    return instances, color_table, errors
