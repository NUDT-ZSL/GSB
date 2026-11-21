from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

import numpy as np

from .errors import E_INCONSISTENT, BusinessError
from .inference import independent_replay
from .manifest import LayerSpec
from .quant import dequantize_layer
from .util import array_digest


STAGE_KEYS = (
    "model_package_validation",
    "layer_quantization",
    "calibration_statistics",
    "layer_inference",
)


def _fail(category: str, message: str, **details: Any) -> None:
    raise BusinessError(
        E_INCONSISTENT,
        message,
        stage="cross_module_self_check",
        details={"category": category, **details},
        trace=["selfcheck:consistency_guard"],
    )


def run_consistency_checks(
    snapshot: Mapping[str, Any],
    config,
) -> dict[str, Any]:
    layer_order = list(snapshot.get("layer_order", []))
    stages = snapshot.get("stages", {})
    checks: list[dict[str, Any]] = []
    stage_sets: dict[str, set[str]] = {}
    for stage in STAGE_KEYS:
        stage_data = stages.get(stage)
        if not stage_data:
            _fail("missing_stage", f"stage is missing: {stage}", stage=stage)
        completed = list(stage_data.get("completed_layers", []))
        stage_sets[stage] = set(completed)
        if set(completed) != set(layer_order):
            _fail(
                "stage_layer_set_mismatch",
                "stage layer set differs from manifest layer order",
                stage=stage,
                completed_layers=completed,
                layer_order=layer_order,
            )
    if len({frozenset(value) for value in stage_sets.values()}) != 1:
        _fail("four_stage_layer_set_mismatch", "four stages processed different layer sets")
    checks.append({"name": "four_stage_layer_sets", "passed": True, "layers": len(layer_order)})

    quant_stage = stages["layer_quantization"]
    quant_layers = quant_stage.get("layers", {})
    validation_layers = stages["model_package_validation"]["layers"]
    for layer_id in layer_order:
        quant_layer = quant_layers.get(layer_id)
        if not quant_layer:
            _fail(
                "missing_quant_layer",
                "quantization layer is missing",
                layer_id=layer_id,
            )
        validation_layer = validation_layers.get(layer_id, {})
        if validation_layer.get("weight_digest") != quant_layer.get("weight_digest"):
            _fail(
                "weight_digest_mismatch",
                "validated weight digest differs from quantified weight digest",
                layer_id=layer_id,
            )
        reconstructed = dequantize_layer(quant_layer, scale_mode="stored")
        committed = np.asarray(quant_layer["dequant_weights"], dtype=np.float64).reshape(
            tuple(quant_layer["layer_shape"])
        )
        if array_digest(reconstructed) != array_digest(committed):
            _fail(
                "quant_dequant_checksum_mismatch",
                "quantization parameters cannot reproduce dequantized weights",
                layer_id=layer_id,
            )
        codes = np.asarray(quant_layer["codes"], dtype=np.int64)
        params = quant_layer["params"]
        qmin = int(params["qmin"])
        qmax = int(params["qmax"])
        if codes.size != committed.size or np.any(codes < qmin) or np.any(codes > qmax):
            _fail(
                "quant_code_range_mismatch",
                "quantized codes are out of range or have the wrong length",
                layer_id=layer_id,
            )
        groups = params.get("groups", [])
        covered = 0
        for group in groups:
            if int(group["start"]) != covered or int(group["end"]) <= covered:
                _fail(
                    "quant_group_coverage_mismatch",
                    "group boundaries are not contiguous",
                    layer_id=layer_id,
                )
            covered = int(group["end"])
        if covered != codes.size:
            _fail(
                "quant_group_coverage_mismatch",
                "group boundaries do not cover every weight",
                layer_id=layer_id,
            )
    checks.append(
        {
            "name": "quant_params_roundtrip",
            "passed": True,
            "layers": len(layer_order),
        }
    )

    for stage in STAGE_KEYS:
        stage_data = stages[stage]
        affected = list(stage_data.get("affected_layers", []))
        reused = list(stage_data.get("reused_layers", []))
        rebuilt = list(stage_data.get("rebuilt_layers", []))
        if set(reused) & set(rebuilt):
            _fail(
                "reuse_rebuild_overlap",
                "recomputed and reused layer sets overlap",
                stage=stage,
                overlap=sorted(set(reused) & set(rebuilt)),
            )
        if set(affected) != set(rebuilt):
            _fail(
                "affected_rebuild_mismatch",
                "affected layer set does not equal the rebuilt suffix",
                stage=stage,
                affected=affected,
                rebuilt=rebuilt,
            )
        if set(reused) | set(rebuilt) != set(layer_order):
            _fail(
                "dependency_coverage_mismatch",
                "reused and rebuilt layers do not cover all layers",
                stage=stage,
                reused=reused,
                rebuilt=rebuilt,
            )
        if set(affected) - (set(reused) | set(rebuilt)):
            _fail(
                "affected_dependency_coverage_mismatch",
                "affected layers are not covered by dependency sets",
                stage=stage,
            )
    checks.append(
        {
            "name": "dependency_reuse_sets",
            "passed": True,
            "stages": len(STAGE_KEYS),
        }
    )

    inference_stage = stages["layer_inference"]
    inference_layers = inference_stage.get("layers", {})
    final_prediction = np.asarray(
        inference_stage["final_prediction"], dtype=np.float64
    )
    last_layer = inference_layers[layer_order[-1]]
    if array_digest(final_prediction) != last_layer["activation_digest"]:
        _fail(
            "final_prediction_activation_mismatch",
            "final prediction is not the last committed activation",
        )
    initial_input = np.asarray(inference_stage["initial_input"], dtype=np.float64)
    for layer_id in layer_order:
        inference_layer = inference_layers.get(layer_id)
        if not inference_layer:
            _fail(
                "missing_inference_layer",
                "inference layer is missing",
                layer_id=layer_id,
            )
        if inference_layer.get("quant_checksum") != quant_layers[layer_id].get(
            "checksum"
        ):
            _fail(
                "inference_quant_source_mismatch",
                "inference did not use the current committed quantization snapshot",
                layer_id=layer_id,
            )
    replayed, replay_records = independent_replay(
        tuple(
            LayerSpec(**validation_layers[layer_id]["spec"]) for layer_id in layer_order
        ),
        quant_layers,
        initial_input,
        config,
    )
    if array_digest(replayed) != last_layer["activation_digest"]:
        _fail(
            "independent_replay_mismatch",
            "final prediction cannot be independently replayed from committed weights",
        )
    if len(replay_records) != len(layer_order):
        _fail("replay_layer_count_mismatch", "independent replay skipped layers")
    snapshot_reference, _ = independent_replay(
        tuple(
            LayerSpec(**validation_layers[layer_id]["spec"]) for layer_id in layer_order
        ),
        quant_layers,
        initial_input,
        replace(config, accumulation_dtype="float64"),
    )
    expected_snapshot_reference = inference_stage.get("diagnostics", {}).get(
        "snapshot_reference", {}
    )
    if array_digest(snapshot_reference) != expected_snapshot_reference.get(
        "prediction_digest"
    ):
        _fail(
            "snapshot_reference_replay_mismatch",
            "float64 committed-weight reference cannot be independently replayed",
        )
    checks.append(
        {
            "name": "independent_prediction_replay",
            "passed": True,
            "layers": len(layer_order),
        }
    )
    checks.append(
        {
            "name": "snapshot_float64_reference_replay",
            "passed": True,
            "layers": len(layer_order),
        }
    )
    return {
        "passed": True,
        "checks": checks,
        "snapshot_version": snapshot.get("version"),
        "result_digest": snapshot.get("result_digest"),
    }
