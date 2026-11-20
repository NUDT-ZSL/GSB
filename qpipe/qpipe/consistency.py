"""Cross-module consistency self-checks.

Every check raises :data:`E_INCONSISTENT` with a ``category`` and the offending
layer index, rather than returning a vague boolean, so failures are directly
attributable.
"""

from __future__ import annotations
from typing import Any, Dict, Set, Tuple

import numpy as np

from .deterministic import checksum_array
from .errors import E_INCONSISTENT, PipelineError
from .infer import forward_layer
from .models import ModelPackage
from .quantize import verify_roundtrip
from .storage import CacheStore, ProgressStore


def _fail(category: str, message: str, layer_index: int) -> None:
    raise PipelineError(
        E_INCONSISTENT,
        message,
        layer_index=layer_index,
        detail={"category": category},
    )


def check_layer_sets(
    package: ModelPackage, cache: CacheStore
) -> Tuple[bool, Dict[str, Any]]:
    """The four stages must have processed exactly the same layer set."""
    expected = set(package.indices())
    per_stage: Dict[str, Set[int]] = {}
    for stage in ("validate", "quantize", "calibrate", "infer"):
        layers = set(cache.list_layers(stage))
        per_stage[stage] = layers
        if layers != expected:
            missing = sorted(expected - layers)
            extra = sorted(layers - expected)
            raise PipelineError(
                E_INCONSISTENT,
                f"stage {stage} layer set mismatch",
                detail={
                    "category": "layer_set",
                    "stage": stage,
                    "missing": missing,
                    "extra": extra,
                },
            )
    return True, {"per_stage": {k: sorted(v) for k, v in per_stage.items()}}


def check_quant_roundtrip(
    package: ModelPackage, cache: CacheStore
) -> Tuple[bool, Dict[str, Any]]:
    """Quant params must reconstruct the stored de-quantised weights."""
    for spec in package.layers:
        entry = cache.read_entry("quantize", spec.layer_index)
        if entry is None:
            _fail("quant_roundtrip", "missing quantize cache", spec.layer_index)
        quant = cache.read_array("quantize", spec.layer_index, "quant")
        dequant = cache.read_array("quantize", spec.layer_index, "dequant")
        payload = entry["payload"]
        verify_roundtrip(
            payload["quant_dtype"],
            payload["zero_point"],
            payload["scale"],
            quant,
            payload["dequant_checksum"],
            layer_index=spec.layer_index,
        )
        if checksum_array(dequant) != payload["dequant_checksum"]:
            _fail(
                "quant_roundtrip",
                "stored dequant checksum mismatch",
                spec.layer_index,
            )
    return True, {"layers_checked": package.layer_count}


def check_recompute_reuse_partition(
    point: Dict[str, Any]
) -> Tuple[bool, Dict[str, Any]]:
    """Recompute and reuse sets are disjoint and cover all affected layers."""
    recomputed = set(point.get("recomputed_layers", []))
    reused = set(point.get("reused_layers", []))
    affected = set(point.get("affected_layers", []))
    overlap = recomputed & reused
    if overlap:
        raise PipelineError(
            E_INCONSISTENT,
            "recompute and reuse sets overlap",
            layer_index=min(overlap),
            detail={"category": "recompute_reuse", "overlap": sorted(overlap)},
        )
    if recomputed | reused != set(point.get("completed_layers", [])):
        raise PipelineError(
            E_INCONSISTENT,
            "recompute/reuse union != completed layers",
            detail={"category": "recompute_reuse"},
        )
    if not affected.issubset(recomputed | reused):
        raise PipelineError(
            E_INCONSISTENT,
            "affected layers not covered by recompute/reuse",
            detail={
                "category": "recompute_reuse",
                "uncovered": sorted(affected - (recomputed | reused)),
            },
        )
    return True, {
        "recomputed": sorted(recomputed),
        "reused": sorted(reused),
    }


def check_independent_prediction(
    package: ModelPackage,
    cache: CacheStore,
    calibration_input: np.ndarray,
) -> Tuple[bool, Dict[str, Any]]:
    """Recompute the prediction independently and compare to the cache."""
    current = np.ascontiguousarray(
        calibration_input[0:1], dtype=np.float32
    )
    last_index = package.layers[-1].layer_index
    for spec in package.layers:
        dequant = cache.read_array("quantize", spec.layer_index, "dequant")
        activation = forward_layer(
            spec,
            current,
            dequant,
            is_last=(spec.layer_index == last_index),
        )
        stored = cache.read_array("infer", spec.layer_index, "activation")
        if checksum_array(activation.output) != checksum_array(stored):
            _fail(
                "independent_prediction",
                "independent forward pass diverges from cached activation",
                spec.layer_index,
            )
        current = activation.output
    return True, {"prediction_checksum": checksum_array(current)}


def run_all_checks(
    package: ModelPackage,
    store: ProgressStore,
    cache: CacheStore,
    calibration_input: np.ndarray,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {"ok": True, "checks": {}}
    _, report["checks"]["layer_sets"] = check_layer_sets(package, cache)
    _, report["checks"]["quant_roundtrip"] = check_quant_roundtrip(
        package, cache
    )
    head = store.head()
    if head is not None:
        _, report["checks"]["recompute_reuse"] = (
            check_recompute_reuse_partition(head)
        )
    _, report["checks"]["independent_prediction"] = check_independent_prediction(
        package, cache, calibration_input
    )
    return report
