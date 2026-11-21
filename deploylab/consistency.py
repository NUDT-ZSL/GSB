from __future__ import annotations

from typing import Any

import numpy as np

from .errors import DeployError, E_INCONSISTENT, STAGES
from .pipeline import deterministic_linear
from .quant import verify_dequant_with_original
from .util import array_checksum, array_digest, decode_array


class ConsistencyChecker:
    def __init__(self, pipeline: Any) -> None:
        self.pipeline = pipeline
        self.store = pipeline.store

    def run(self, *, raise_on_error: bool = True) -> dict[str, Any]:
        categories: dict[str, Any] = {}
        problems: list[str] = []
        latest = self.store.latest_generation()
        if latest is None or not latest["completed"]:
            problem = "no completed generation is available"
            if raise_on_error:
                raise DeployError(
                    E_INCONSISTENT,
                    "inference",
                    problem,
                    details={"categories": {"completion": [problem]}},
                )
            return {
                "ok": False,
                "categories": {"completion": [problem]},
                "problems": [problem],
            }
        generation = latest["generation"]
        bundle = self.pipeline._load_bundle()
        snapshots = self.store.generation_snapshots(generation)
        by_stage = {snapshot["stage"]: snapshot for snapshot in snapshots}
        manifest_layers = {
            layer["layer_id"]: layer for layer in latest["manifest"]["layers"]
        }
        expected_ids = sorted(manifest_layers)

        stage_problems: list[str] = []
        if set(by_stage) != set(STAGES):
            stage_problems.append(f"committed stages are {sorted(by_stage)}")
        for stage in STAGES:
            snapshot = by_stage.get(stage)
            if snapshot is None:
                continue
            if sorted(snapshot["completed_layers"]) != expected_ids:
                stage_problems.append(
                    f"{stage} completed_layers {snapshot['completed_layers']} != "
                    f"{expected_ids}"
                )
        chain_problems = self.store.verify_chain()
        stage_problems.extend(chain_problems)
        categories["stage_layer_sets_and_chain"] = stage_problems
        problems.extend(stage_problems)

        quant_artifacts = self.store.load_stage_artifacts(generation, "quantization")
        calibration_artifacts = self.store.load_stage_artifacts(
            generation, "calibration"
        )
        inference_artifacts = self.store.load_stage_artifacts(generation, "inference")
        quant_problems: list[str] = []
        calib_problems: list[str] = []
        for layer_id in expected_ids:
            layer_record = manifest_layers[layer_id]
            quant_row = quant_artifacts.get(layer_id)
            calib_row = calibration_artifacts.get(layer_id)
            if quant_row is None:
                quant_problems.append(f"{layer_id}: quantization artifact missing")
                continue
            payload = quant_row["payload"]
            meta = payload.get("quant") or {}
            if int(payload.get("weight_revision", -1)) != int(
                layer_record["weight_revision"]
            ):
                quant_problems.append(
                    f"{layer_id}: quant revision {payload.get('weight_revision')} "
                    f"!= manifest {layer_record['weight_revision']}"
                )
            try:
                dequant = decode_array(payload["dequant"])
                bias = decode_array(payload["bias"])
            except Exception as exc:
                quant_problems.append(f"{layer_id}: artifact decode failed: {exc}")
                continue
            if array_digest(dequant) != meta.get("dequant_digest"):
                quant_problems.append(
                    f"{layer_id}: dequant payload does not match metadata digest"
                )
            if array_digest(bias) != meta.get("bias_digest"):
                quant_problems.append(
                    f"{layer_id}: bias payload does not match metadata digest"
                )
            layer = bundle.layer_by_id[layer_id]
            original, _ = bundle.load_weight(layer)
            try:
                if not verify_dequant_with_original(layer, original, payload):
                    quant_problems.append(
                        f"{layer_id}: quantization parameters cannot reproduce "
                        "the dequantized weight"
                    )
            except Exception as exc:
                quant_problems.append(
                    f"{layer_id}: quantization replay failed: {exc}"
                )
            if calib_row is None:
                calib_problems.append(f"{layer_id}: calibration artifact missing")
            else:
                calib_payload = calib_row["payload"]
                if int(calib_payload.get("weight_revision", -1)) != int(
                    layer_record["weight_revision"]
                ):
                    calib_problems.append(
                        f"{layer_id}: calibration revision is stale"
                    )
                if int(calib_payload.get("calibration_samples", -1)) != int(
                    bundle.calib_samples
                ):
                    calib_problems.append(
                        f"{layer_id}: calibration sample count does not match bundle"
                    )
        categories["quant_params_match_dequant"] = quant_problems
        categories["calibration_snapshot"] = calib_problems
        problems.extend(quant_problems)
        problems.extend(calib_problems)

        invalidation = latest["invalidation"]
        affected = set(invalidation.get("affected_layers", []))
        reused = set(invalidation.get("reused_layers", []))
        recomputed = set(invalidation.get("recomputed_layers", []))
        invalidation_problems: list[str] = []
        if affected & reused:
            invalidation_problems.append(
                f"affected and reused sets overlap: {sorted(affected & reused)}"
            )
        if affected | reused != set(expected_ids):
            invalidation_problems.append(
                "affected/reused union does not cover all layers"
            )
        if recomputed != affected:
            invalidation_problems.append(
                "recomputed layer set does not equal affected layer set"
            )
        changed = set(invalidation.get("changed_layers", []))
        if changed:
            first_index = min(
                int(manifest_layers[layer_id]["layer_index"])
                for layer_id in changed
                if layer_id in manifest_layers
            )
            expected_affected = {
                layer_id
                for layer_id, record in manifest_layers.items()
                if int(record["layer_index"]) >= first_index
            }
            if affected != expected_affected:
                invalidation_problems.append(
                    "affected set is not the changed layer plus all downstream layers"
                )
        categories["invalidation_sets"] = invalidation_problems
        problems.extend(invalidation_problems)

        inference_problems: list[str] = []
        activation = bundle.load_initial_input()
        if activation.shape != (bundle.layers[0].in_features,):
            inference_problems.append("initial input dimension mismatch")
        for layer in bundle.layers:
            quant_row = quant_artifacts.get(layer.layer_id)
            inference_row = inference_artifacts.get(layer.layer_id)
            if quant_row is None or inference_row is None:
                inference_problems.append(
                    f"{layer.layer_id}: missing inference or quant artifact"
                )
                continue
            quant_payload = quant_row["payload"]
            inference_payload = inference_row["payload"]
            if inference_payload.get("quant_artifact_digest") != quant_row["digest"]:
                inference_problems.append(
                    f"{layer.layer_id}: inference did not use the committed "
                    "quantization snapshot"
                )
            if int(quant_payload.get("weight_revision", -1)) != layer.weight_revision:
                inference_problems.append(
                    f"{layer.layer_id}: stale weight revision in inference chain"
                )
            weight = decode_array(quant_payload["dequant"])
            bias = decode_array(quant_payload["bias"])
            linear = deterministic_linear(weight, activation, bias)
            activation = np.ascontiguousarray(np.tanh(linear), dtype="<f8")
            if array_digest(activation) != inference_payload.get("output_digest"):
                inference_problems.append(
                    f"{layer.layer_id}: independently recomputed activation "
                    "digest differs from snapshot"
                )
            if array_checksum(activation) != inference_payload.get(
                "activation_checksum"
            ):
                inference_problems.append(
                    f"{layer.layer_id}: activation checksum mismatch"
                )
        inference_snapshot = by_stage.get("inference")
        if inference_snapshot is not None:
            final = inference_artifacts.get(bundle.layers[-1].layer_id)
            if final is not None:
                recorded = final["payload"]["activation"]
                if array_digest(activation) != recorded["digest"]:
                    inference_problems.append(
                        "final prediction bytes differ from independent recomputation"
                    )
                if inference_snapshot["metadata"].get("prediction_digest") != array_digest(
                    activation
                ):
                    inference_problems.append(
                        "inference snapshot prediction digest is inconsistent"
                    )
        categories["independent_inference_replay"] = inference_problems
        problems.extend(inference_problems)

        if problems and raise_on_error:
            raise DeployError(
                E_INCONSISTENT,
                "inference",
                "cross-module consistency check failed: "
                + "; ".join(problems[:8]),
                details={"categories": categories, "problems": problems},
            )
        return {
            "ok": not problems,
            "generation": generation,
            "categories": categories,
            "problems": problems,
            "prediction_digest": array_digest(activation),
            "prediction_checksum": array_checksum(activation),
            "activation_checksums": {
                layer.layer_id: array_checksum(
                    decode_array(inference_artifacts[layer.layer_id]["payload"]["activation"])
                )
                for layer in bundle.layers
                if layer.layer_id in inference_artifacts
            },
        }

