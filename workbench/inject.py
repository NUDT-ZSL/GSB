"""Standalone fault-injection configuration handling."""

from __future__ import annotations

import json

from .errors import (E_CONFIDENCE_RANGE, E_PARSE_INVALID, E_REGION_INVALID,
                     E_RELATION_DANGLING)

EXPECTED_CODES = {
    "missing_field_line": E_PARSE_INVALID,
    "invalid_json": E_PARSE_INVALID,
    "bbox_out_of_bounds": E_REGION_INVALID,
    "confidence_out_of_range": E_CONFIDENCE_RANGE,
    "dangling_relation": E_RELATION_DANGLING,
}


def apply_injection(spec: dict, v1_text: str, v2_text: str,
                    image_width: int) -> tuple[str, str]:
    """Return mutated (v1_text, v2_text) for one injection spec."""
    kind = spec["type"]
    if kind == "missing_field_line":
        lines = v1_text.splitlines()
        idx = int(spec.get("line", 1)) - 1
        lines[idx] = lines[idx][:20]  # truncate: missing trailing fields
        return "\n".join(lines) + "\n", v2_text
    if kind == "invalid_json":
        cut = max(1, int(len(v2_text) * 0.6))
        return v1_text, v2_text[:cut] + ", {broken"
    doc = json.loads(v2_text)
    if kind == "bbox_out_of_bounds":
        for e in doc:
            if e.get("id") == spec["inst"]:
                e["bbox"][2] = float(image_width) + 50.5
        return v1_text, json.dumps(doc, indent=2)
    if kind == "confidence_out_of_range":
        for e in doc:
            if e.get("id") == spec["inst"]:
                e["confidence"] = 1.7
        return v1_text, json.dumps(doc, indent=2)
    if kind == "dangling_relation":
        for e in doc:
            if e.get("id") == spec["inst"]:
                e.setdefault("relations", []).append("R999")
        return v1_text, json.dumps(doc, indent=2)
    raise ValueError(f"unknown injection type: {kind}")
