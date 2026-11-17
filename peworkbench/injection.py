"""Fault-injection manifest handling.

Injection entries mutate real inputs (features, positions, per-sample
base/scale overrides); the pipeline validators then detect the faults and
emit the corresponding business codes.
"""

import copy
import math

from . import (
    E_BASE_ZERO,
    E_NONFINITE_FEATURE,
    E_POS_OUT_OF_RANGE,
    E_SCALE_INVALID,
)

FAULT_TO_CODE = {
    "nonfinite_feature": E_NONFINITE_FEATURE,
    "pos_out_of_range": E_POS_OUT_OF_RANGE,
    "base_zero": E_BASE_ZERO,
    "scale_invalid": E_SCALE_INVALID,
}

_NONFINITE_VALUES = {
    "nan": math.nan,
    "inf": math.inf,
    "-inf": -math.inf,
}


def apply_injections(samples, manifest):
    """Return (mutated_samples, param_overrides, hits, faulted_ids)."""
    mutated = copy.deepcopy(samples)
    index = {s["sample_id"]: s for s in mutated}
    overrides = {}
    hits = []
    entries = sorted(manifest.get("entries", []), key=lambda e: e["inj_id"])
    for entry in entries:
        fault = entry["fault"]
        code = FAULT_TO_CODE.get(fault)
        sample = index.get(entry["sample_id"])
        hit = {
            "inj_id": entry["inj_id"],
            "sample_id": entry["sample_id"],
            "stage": entry.get("stage"),
            "fault": fault,
            "code": code,
            "applied": False,
        }
        if sample is None or code is None:
            hits.append(hit)
            continue
        detail = entry.get("detail", {})
        if fault == "nonfinite_feature":
            value = _NONFINITE_VALUES[detail.get("value", "nan")]
            component = detail.get("component", "query")
            dim_index = int(detail.get("dim_index", 0))
            if component == "query":
                sample["features"]["query"][dim_index] = value
            else:
                key_index = int(detail.get("key_index", 0))
                sample["features"]["keys"][key_index][dim_index] = value
        elif fault == "pos_out_of_range":
            key_index = int(detail.get("key_index", 0))
            sample["key_positions"][key_index] = int(detail["position"])
        elif fault == "base_zero":
            overrides.setdefault(sample["sample_id"], {})["base"] = 0.0
        elif fault == "scale_invalid":
            overrides.setdefault(sample["sample_id"], {})["scale"] = float(
                detail.get("scale_factor", -1.0))
        hit["applied"] = True
        hits.append(hit)
    faulted_ids = {h["sample_id"] for h in hits if h["applied"]}
    return mutated, overrides, hits, faulted_ids
