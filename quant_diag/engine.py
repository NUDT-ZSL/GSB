from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .calibration import calibration_statistics
from .config import EngineConfig
from .diagnostics import compute_error_spectrum
from .errors import (
    E_INVALID_MANIFEST,
    E_REVISION_ROLLBACK,
    E_RESOURCE_LIMIT,
    BusinessError,
)
from .inference import infer_one_layer
from .manifest import LayerSpec, load_manifest
from .quant import finalize_quant_layer, quantize_weight
from .sample_data import (
    CalibrationSample,
    calibration_vectors,
    load_calibration,
    sample_layer_digest,
    samples_by_layer,
)
from .snapshots import STAGE_BY_INDEX, SnapshotStore
from .util import array_digest, copy_json, json_digest
from .weights import generate_weight, prepare_weight, weight_checksum


STAGE_VALIDATION = "model_package_validation"
STAGE_QUANTIZATION = "layer_quantization"
STAGE_CALIBRATION = "calibration_statistics"
STAGE_INFERENCE = "layer_inference"
def _seal_stage(data: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(data)
    payload = {key: value for key, value in sealed.items() if key != "checksum"}
    sealed["checksum"] = json_digest(payload)
    return sealed


def _suffix(layer_ids: list[str], start: int) -> list[str]:
    return list(layer_ids[start:])


class DiagnosticEngine:
    def __init__(
        self,
        *,
        manifest_path: Path,
        calibration_path: Path,
        runtime_dir: Path,
        config: EngineConfig | None = None,
        force_kill_after: tuple[str, int] | None = None,
    ):
        self.manifest_path = Path(manifest_path)
        self.calibration_path = Path(calibration_path)
        self.runtime_dir = Path(runtime_dir)
        self.config = config or EngineConfig()
        self.config.validate()
        self.manifest = load_manifest(self.manifest_path)
        self.samples = load_calibration(self.calibration_path, self.manifest)
        self.store = SnapshotStore(self.runtime_dir)
        self.force_kill_after = force_kill_after
        self.layer_ids = self.manifest.layer_ids
        self.layer_index = {
            layer.layer_id: layer.layer_index for layer in self.manifest.layers
        }
        self._degradations: list[dict[str, Any]] = []

    def _check_stage_layer_budget(self, stage: str) -> None:
        if len(self.manifest.layers) > self.config.max_layers_per_stage:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "stage layer budget exceeded",
                stage=stage,
                details={
                    "layer_count": len(self.manifest.layers),
                    "max_layers_per_stage": self.config.max_layers_per_stage,
                },
                trace=["engine:layer_budget_guard"],
            )

    def _mark_inflight(self, stage: str, layer: LayerSpec) -> None:
        self.store.write_inflight(
            {
                "status": "inflight",
                "committed": False,
                "stage": stage,
                "layer_id": layer.layer_id,
                "layer_index": layer.layer_index,
                "note": "not part of the committed progress view",
            }
        )

    def _maybe_force_kill(self, stage: str, layer: LayerSpec) -> None:
        if self.force_kill_after is None:
            return
        kill_stage, kill_layer_index = self.force_kill_after
        if stage != kill_stage or layer.layer_index != kill_layer_index:
            return
        os._exit(137)

    def _compute_validation_stage(self) -> dict[str, Any]:
        self._check_stage_layer_budget(STAGE_VALIDATION)
        layers: dict[str, Any] = {}
        for layer in self.manifest.layers:
            self._mark_inflight(STAGE_VALIDATION, layer)
            digest = weight_checksum(generate_weight(layer))
            layers[layer.layer_id] = {
                "layer_id": layer.layer_id,
                "layer_index": layer.layer_index,
                "spec": layer.as_dict(),
                "weight_digest": digest,
                "weight_shape": list(layer.layer_shape),
                "weight_elements": int(np.prod(layer.layer_shape)),
            }
            self._maybe_force_kill(STAGE_VALIDATION, layer)
        return _seal_stage(
            {
                "completed_layers": list(self.layer_ids),
                "manifest_digest": self.manifest.digest,
                "config_digest": self.config.digest,
                "config": self.config.as_dict(),
                "layers": layers,
                "affected_layers": list(self.layer_ids),
                "reused_layers": [],
                "rebuilt_layers": list(self.layer_ids),
            }
        )

    @staticmethod
    def _stage_reusable(
        previous: Mapping[str, Any] | None,
        stage: str,
        manifest_digest: str,
        config_digest: str,
        layer_ids: list[str],
    ) -> bool:
        if not previous:
            return False
        if previous.get("manifest_digest") != manifest_digest:
            return False
        if previous.get("config_digest") != config_digest:
            return False
        stage_data = previous.get("stages", {}).get(stage)
        return bool(
            stage_data
            and set(stage_data.get("completed_layers", [])) == set(layer_ids)
        )

    def _revision_affected(self, previous: Mapping[str, Any] | None) -> list[str]:
        if not previous:
            return list(self.layer_ids)
        previous_stage = previous.get("stages", {}).get(STAGE_VALIDATION)
        if not previous_stage:
            return list(self.layer_ids)
        if previous.get("config_digest") != self.config.digest:
            return list(self.layer_ids)
        previous_layers = previous_stage.get("layers", {})
        previous_ids = previous_stage.get("completed_layers", [])
        if set(previous_ids) != set(self.layer_ids):
            raise BusinessError(
                E_INVALID_MANIFEST,
                "layer set changed between model package revisions",
                stage=STAGE_VALIDATION,
                details={
                    "previous_layers": sorted(previous_ids),
                    "current_layers": sorted(self.layer_ids),
                },
            )
        earliest: int | None = None
        for layer in self.manifest.layers:
            prior = previous_layers.get(layer.layer_id)
            if not prior:
                earliest = layer.layer_index
                break
            previous_spec = LayerSpec(**prior["spec"])
            if layer.weight_revision < previous_spec.weight_revision:
                raise BusinessError(
                    E_REVISION_ROLLBACK,
                    "weight_revision moved backwards",
                    stage=STAGE_VALIDATION,
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    details={
                        "previous_revision": previous_spec.weight_revision,
                        "current_revision": layer.weight_revision,
                    },
                    trace=["engine:revision_monotonicity_guard"],
                )
            if layer.weight_identity() != previous_spec.weight_identity():
                if layer.weight_revision <= previous_spec.weight_revision:
                    raise BusinessError(
                        E_REVISION_ROLLBACK,
                        "changed weight content requires a higher weight_revision",
                        stage=STAGE_VALIDATION,
                        layer_id=layer.layer_id,
                        layer_index=layer.layer_index,
                        details={
                            "previous_revision": previous_spec.weight_revision,
                            "current_revision": layer.weight_revision,
                        },
                        trace=["engine:revision_monotonicity_guard"],
                    )
                if earliest is None or layer.layer_index < earliest:
                    earliest = layer.layer_index
        if earliest is None:
            return []
        return _suffix(self.layer_ids, earliest)

    def _compute_initial_input(self) -> np.ndarray:
        grouped = samples_by_layer(self.samples, self.manifest)
        first = self.manifest.layers[0]
        candidates = grouped.get(first.layer_id, [])
        if not candidates:
            raise BusinessError(
                "E_CALIBRATION_SHAPE_MISMATCH",
                "no calibration input is available for the first layer",
                stage=STAGE_INFERENCE,
                layer_id=first.layer_id,
                layer_index=0,
            )
        vector = np.asarray(candidates[0].vector, dtype=np.float64)
        if self.config.input_override is not None:
            vector = np.asarray(self.config.input_override, dtype=np.float64)
        return vector

    def _calibration_affected(
        self,
        previous: Mapping[str, Any] | None,
        grouped: Mapping[str, list[CalibrationSample]],
    ) -> tuple[list[str], dict[str, str]]:
        digests = {
            layer_id: sample_layer_digest(grouped.get(layer_id, []))
            for layer_id in self.layer_ids
        }
        if not previous:
            return list(self.layer_ids), digests
        if previous.get("config_digest") != self.config.digest:
            return list(self.layer_ids), digests
        previous_stage = previous.get("stages", {}).get(STAGE_CALIBRATION)
        if not previous_stage:
            return list(self.layer_ids), digests
        previous_layers = previous_stage.get("layers", {})
        earliest: int | None = None
        for layer in self.manifest.layers:
            prior = previous_layers.get(layer.layer_id)
            if not prior or prior.get("sample_digest") != digests[layer.layer_id]:
                earliest = layer.layer_index
                break
        if earliest is None:
            return [], digests
        return _suffix(self.layer_ids, earliest), digests

    def _degradation_plan(self) -> tuple[int, list[dict[str, Any]]]:
        elements_by_layer = {
            layer.layer_id: int(np.prod(layer.layer_shape))
            for layer in self.manifest.layers
        }
        limit_bytes = int(self.config.max_resident_weight_mb * 1024 * 1024)
        per_layer_peak = max(elements_by_layer.values()) * 24
        total_peak = sum(elements_by_layer.values()) * 24
        largest_layer = max(
            self.manifest.layers,
            key=lambda layer: elements_by_layer[layer.layer_id],
        )
        if per_layer_peak > limit_bytes:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "resident dequantized weight memory exceeds the hard budget",
                stage=STAGE_QUANTIZATION,
                layer_id=largest_layer.layer_id,
                layer_index=largest_layer.layer_index,
                details={
                    "estimated_layer_bytes": per_layer_peak,
                    "max_resident_weight_bytes": limit_bytes,
                },
                trace=["engine:resident_memory_budget"],
            )
        if total_peak > limit_bytes:
            degradation = {
                "stage": STAGE_QUANTIZATION,
                "layer_id": "*",
                "category": "grouped_quantization",
                "detail": (
                    "total-weight residency forecast exceeded budget; "
                    "streaming group quantization enabled"
                ),
                "estimated_total_bytes": total_peak,
                "max_resident_weight_bytes": limit_bytes,
                "group_size_before": self.config.group_size,
                "group_size_after": self.config.degrade_group_size,
            }
            return self.config.degrade_group_size, [degradation]
        return self.config.group_size, []

    def _compute_quantization_stage(
        self,
        previous: Mapping[str, Any] | None,
        revision_affected: list[str],
    ) -> dict[str, Any]:
        self._check_stage_layer_budget(STAGE_QUANTIZATION)
        group_size, degradations = self._degradation_plan()
        self._degradations = degradations
        previous_stage = (
            previous.get("stages", {}).get(STAGE_QUANTIZATION) if previous else None
        )
        previous_layers = previous_stage.get("layers", {}) if previous_stage else {}
        affected_set = set(revision_affected)
        reused: list[str] = []
        rebuilt: list[str] = []
        layers: dict[str, Any] = {}
        for layer in self.manifest.layers:
            self._mark_inflight(STAGE_QUANTIZATION, layer)
            prior = previous_layers.get(layer.layer_id)
            can_reuse = (
                layer.layer_id not in affected_set
                and prior is not None
                and previous_stage is not None
                and previous_stage.get("config_digest") == self.config.digest
            )
            if can_reuse:
                layers[layer.layer_id] = copy_json(prior)
                reused.append(layer.layer_id)
                self._maybe_force_kill(STAGE_QUANTIZATION, layer)
                continue
            injected_layer = layer
            if self.config.injection("unknown_dtype", layer.layer_id):
                injected_layer = replace(layer, quant_dtype="mystery8")
            weight, digest = prepare_weight(
                layer,
                truncate=self.config.injection("weight_truncate", layer.layer_id),
            )
            quant_layer = quantize_weight(
                weight,
                injected_layer,
                self.config,
                group_size=group_size,
            )
            quant_layer = finalize_quant_layer(quant_layer, weight, digest)
            layers[layer.layer_id] = quant_layer
            rebuilt.append(layer.layer_id)
            self._maybe_force_kill(STAGE_QUANTIZATION, layer)
        stage = _seal_stage(
            {
                "completed_layers": list(self.layer_ids),
                "manifest_digest": self.manifest.digest,
                "config_digest": self.config.digest,
                "layers": layers,
                "affected_layers": list(revision_affected),
                "reused_layers": reused,
                "rebuilt_layers": rebuilt,
                "degradations": degradations,
            }
        )
        return stage

    def _compute_calibration_stage(
        self,
        previous: Mapping[str, Any] | None,
        affected: list[str],
        grouped: Mapping[str, list[CalibrationSample]],
        sample_digests: Mapping[str, str],
    ) -> dict[str, Any]:
        self._check_stage_layer_budget(STAGE_CALIBRATION)
        previous_stage = (
            previous.get("stages", {}).get(STAGE_CALIBRATION) if previous else None
        )
        previous_layers = previous_stage.get("layers", {}) if previous_stage else {}
        affected_set = set(affected)
        reused: list[str] = []
        rebuilt: list[str] = []
        layers: dict[str, Any] = {}
        total_comparisons = 0
        for layer in self.manifest.layers:
            self._mark_inflight(STAGE_CALIBRATION, layer)
            prior = previous_layers.get(layer.layer_id)
            can_reuse = (
                layer.layer_id not in affected_set
                and prior is not None
                and prior.get("sample_digest") == sample_digests[layer.layer_id]
                and previous_stage.get("config_digest") == self.config.digest
            )
            if can_reuse:
                layers[layer.layer_id] = copy_json(prior)
                reused.append(layer.layer_id)
                self._maybe_force_kill(STAGE_CALIBRATION, layer)
                continue
            vectors = calibration_vectors(
                grouped.get(layer.layer_id, []),
                self.manifest,
                layer,
                self.config,
                force_count_mismatch=self.config.injection(
                    "calibration_count_mismatch", layer.layer_id
                ),
            )
            total_comparisons += int(vectors.size)
            if total_comparisons > self.config.max_calibration_comparisons:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "calibration comparison budget exceeded for the stage",
                    stage=STAGE_CALIBRATION,
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    details={
                        "comparisons": total_comparisons,
                        "max_calibration_comparisons": self.config.max_calibration_comparisons,
                    },
                    trace=["engine:calibration_comparison_budget"],
                )
            stats = calibration_statistics(
                vectors,
                layer,
                self.config,
                sample_digest=sample_digests[layer.layer_id],
            )
            layers[layer.layer_id] = stats
            rebuilt.append(layer.layer_id)
            self._maybe_force_kill(STAGE_CALIBRATION, layer)
        return _seal_stage(
            {
                "completed_layers": list(self.layer_ids),
                "manifest_digest": self.manifest.digest,
                "config_digest": self.config.digest,
                "layers": layers,
                "sample_digests": dict(sample_digests),
                "affected_layers": list(affected),
                "reused_layers": reused,
                "rebuilt_layers": rebuilt,
                "total_comparisons": total_comparisons,
            }
        )

    def _initial_input_for_inference(self) -> np.ndarray:
        return self._compute_initial_input()

    def _compute_inference_stage(
        self,
        previous: Mapping[str, Any] | None,
        affected: list[str],
        committed_quant_stage: Mapping[str, Any],
        initial_input: np.ndarray,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._check_stage_layer_budget(STAGE_INFERENCE)
        quant_layers = committed_quant_stage["layers"]
        previous_stage = (
            previous.get("stages", {}).get(STAGE_INFERENCE) if previous else None
        )
        previous_layers = previous_stage.get("layers", {}) if previous_stage else {}
        affected_set = set(affected)
        reused: list[str] = []
        rebuilt: list[str] = []
        layers: dict[str, Any] = {}
        vector = np.asarray(initial_input, dtype=np.float64)
        for layer in self.manifest.layers:
            self._mark_inflight(STAGE_INFERENCE, layer)
            quant_layer = quant_layers.get(layer.layer_id)
            if quant_layer is None:
                raise BusinessError(
                    "E_LAYER_INCOMPLETE",
                    "committed quantization parameters are missing for this layer",
                    stage=STAGE_INFERENCE,
                    layer_id=layer.layer_id,
                    layer_index=layer.layer_index,
                    details={"missing_layer": layer.layer_id},
                    trace=["engine:inference_quantization_guard"],
                )
            prior = previous_layers.get(layer.layer_id)
            can_reuse = (
                layer.layer_id not in affected_set
                and prior is not None
                and prior.get("quant_checksum") == quant_layer["checksum"]
                and prior.get("weight_digest") == quant_layer["dequant_digest"]
                and prior.get("input_digest") == array_digest(vector)
                and previous_stage.get("config_digest") == self.config.digest
            )
            if can_reuse:
                layer_record = copy_json(prior)
                layers[layer.layer_id] = layer_record
                reused.append(layer.layer_id)
                vector = np.asarray(layer_record["activation"], dtype=np.float64)
                self._maybe_force_kill(STAGE_INFERENCE, layer)
                continue
            layer_input = vector
            if self.config.injection("inference_input_dim", layer.layer_id):
                layer_input = (
                    vector[:-1] if vector.size > 1 else np.zeros(vector.size + 1)
                )
            layer_record = infer_one_layer(
                quant_layers, layer, layer_input, self.config
            )
            layers[layer.layer_id] = layer_record
            rebuilt.append(layer.layer_id)
            vector = np.asarray(layer_record["activation"], dtype=np.float64)
            self._maybe_force_kill(STAGE_INFERENCE, layer)
        committed_activations = {
            layer_id: record["activation"] for layer_id, record in layers.items()
        }
        diagnostics = compute_error_spectrum(
            self.manifest.layers,
            quant_layers,
            initial_input,
            self.config,
            committed_activations=committed_activations,
        )
        stage = _seal_stage(
            {
                "completed_layers": list(self.layer_ids),
                "manifest_digest": self.manifest.digest,
                "config_digest": self.config.digest,
                "layers": layers,
                "initial_input": initial_input.tolist(),
                "initial_input_digest": array_digest(initial_input),
                "final_prediction": list(layers[self.layer_ids[-1]]["activation"]),
                "final_prediction_digest": layers[self.layer_ids[-1]]["activation_digest"],
                "diagnostics": diagnostics,
                "affected_layers": list(affected),
                "reused_layers": reused,
                "rebuilt_layers": rebuilt,
            }
        )
        return stage, diagnostics

    @staticmethod
    def _result_digest(
        manifest_digest: str,
        config_digest: str,
        stages: Mapping[str, Any],
    ) -> str:
        quant_stage = stages.get(STAGE_QUANTIZATION, {})
        calib_stage = stages.get(STAGE_CALIBRATION, {})
        inference_stage = stages.get(STAGE_INFERENCE, {})
        payload = {
            "manifest_digest": manifest_digest,
            "config_digest": config_digest,
            "quantization": {
                layer_id: {
                    "checksum": layer.get("checksum"),
                    "dequant_digest": layer.get("dequant_digest"),
                }
                for layer_id, layer in sorted(
                    quant_stage.get("layers", {}).items()
                )
            },
            "calibration": {
                layer_id: layer.get("checksum")
                for layer_id, layer in sorted(
                    calib_stage.get("layers", {}).items()
                )
            },
            "inference": {
                layer_id: {
                    "checksum": layer.get("checksum"),
                    "activation_digest": layer.get("activation_digest"),
                }
                for layer_id, layer in sorted(
                    inference_stage.get("layers", {}).items()
                )
            },
            "diagnostics_checksum": inference_stage.get("diagnostics", {}).get(
                "checksum"
            ),
            "final_prediction_digest": inference_stage.get(
                "final_prediction_digest"
            ),
        }
        return json_digest(payload)

    def _snapshot_summary(self, snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
        if not snapshot:
            return {}
        inference_stage = snapshot.get("stages", {}).get(STAGE_INFERENCE, {})
        diagnostics = inference_stage.get("diagnostics", {})
        return {
            "version": snapshot.get("version"),
            "stage": snapshot.get("stage"),
            "model_tag": snapshot.get("model_tag"),
            "result_digest": snapshot.get("result_digest"),
            "final_prediction_digest": inference_stage.get(
                "final_prediction_digest"
            ),
            "dominant_source": diagnostics.get("overall_dominant_source"),
            "final_serial_error": diagnostics.get("final_serial_error"),
            "degradations": snapshot.get("degradations", []),
        }

    def _recovery_report(
        self,
        before: Mapping[str, Any] | None,
        inflight: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not before:
            resume_stage = STAGE_VALIDATION
            resume_layer = self.layer_ids[0]
            resume_index = 0
        else:
            stage_index = int(before.get("stage_index", 0))
            if stage_index >= 4:
                resume_stage = "complete"
                resume_layer = None
                resume_index = None
            else:
                resume_stage = STAGE_BY_INDEX[stage_index + 1]
                resume_layer = self.layer_ids[0]
                resume_index = 0
        return {
            "latest_committed": (
                {
                    "version": before.get("version"),
                    "stage": before.get("stage"),
                    "completed_layers": before.get("completed_layers", []),
                    "content_checksum": before.get("content_checksum"),
                }
                if before
                else None
            ),
            "discarded_uncommitted": (
                dict(inflight)
                if inflight
                else None
            ),
            "resume_stage": resume_stage,
            "resume_layer_id": resume_layer,
            "resume_layer_index": resume_index,
            "policy": (
                "stage snapshots are all-or-nothing; uncommitted in-flight layer "
                "results are discarded and the next stage is rebuilt from its first layer"
            ),
        }

    def run(self) -> dict[str, Any]:
        before = self.store.load_latest()
        cache = before
        if before and STAGE_QUANTIZATION not in before.get("stages", {}):
            cache = self.store.load_latest_with_stage(STAGE_QUANTIZATION)
        inflight = self.store.read_inflight()
        recovery = self._recovery_report(before, inflight)
        if inflight:
            self.store.clear_inflight()
        grouped = samples_by_layer(self.samples, self.manifest)
        calibration_affected, sample_digests = self._calibration_affected(
            cache, grouped
        )
        revision_affected = self._revision_affected(cache)
        affected_union = sorted(
            set(revision_affected) | set(calibration_affected),
            key=lambda layer_id: self.layer_index[layer_id],
        )
        actions: list[dict[str, Any]] = []
        stage_versions: dict[str, int] = {}
        stages: dict[str, Any] = {}
        before_stage_data = before.get("stages", {}) if before else {}
        previous_stage_data = cache.get("stages", {}) if cache else {}
        try:
            validation_reusable = self._stage_reusable(
                before,
                STAGE_VALIDATION,
                self.manifest.digest,
                self.config.digest,
                self.layer_ids,
            )
            if validation_reusable and before is not None:
                validation_stage = copy_json(
                    before_stage_data[STAGE_VALIDATION]
                )
                stages[STAGE_VALIDATION] = validation_stage
                actions.append(
                    {
                        "stage": STAGE_VALIDATION,
                        "action": "reuse",
                        "version": before.get("version"),
                    }
                )
            else:
                validation_stage = self._compute_validation_stage()
                result_digest = self._result_digest(
                    self.manifest.digest, self.config.digest, {STAGE_VALIDATION: validation_stage}
                )
                snapshot = self.store.commit(
                    stage=STAGE_VALIDATION,
                    model_tag=self.manifest.model_tag,
                    manifest_digest=self.manifest.digest,
                    config_digest=self.config.digest,
                    layer_order=self.layer_ids,
                    completed_layers=validation_stage["completed_layers"],
                    stages={STAGE_VALIDATION: validation_stage},
                    dependency={
                        "affected_layers": validation_stage["affected_layers"],
                        "reused_layers": validation_stage["reused_layers"],
                        "rebuilt_layers": validation_stage["rebuilt_layers"],
                    },
                    result_digest=result_digest,
                    degradations=[],
                    injections=[
                        injection.as_dict() for injection in self.config.injections
                    ],
                    version_hint=(before["version"] + 1) if before else 1,
                    parent_checksum=(
                        before.get("content_checksum") if before else None
                    ),
                )
                stages[STAGE_VALIDATION] = validation_stage
                stage_versions[STAGE_VALIDATION] = snapshot["version"]
                actions.append(
                    {
                        "stage": STAGE_VALIDATION,
                        "action": "commit",
                        "version": snapshot["version"],
                    }
                )

            quantization_reusable = self._stage_reusable(
                before,
                STAGE_QUANTIZATION,
                self.manifest.digest,
                self.config.digest,
                self.layer_ids,
            )
            if quantization_reusable and not revision_affected:
                quantization_stage = copy_json(
                    previous_stage_data[STAGE_QUANTIZATION]
                )
                self._degradations = copy_json(cache.get("degradations", []))
                stages[STAGE_QUANTIZATION] = quantization_stage
                actions.append(
                    {
                        "stage": STAGE_QUANTIZATION,
                        "action": "reuse",
                        "version": before.get("version"),
                    }
                )
            else:
                quantization_affected = (
                    revision_affected
                    if previous_stage_data.get(STAGE_QUANTIZATION)
                    else list(self.layer_ids)
                )
                quantization_stage = self._compute_quantization_stage(
                    cache, quantization_affected
                )
                stages[STAGE_QUANTIZATION] = quantization_stage
                result_digest = self._result_digest(
                    self.manifest.digest, self.config.digest, stages
                )
                latest = self.store.load_latest()
                snapshot = self.store.commit(
                    stage=STAGE_QUANTIZATION,
                    model_tag=self.manifest.model_tag,
                    manifest_digest=self.manifest.digest,
                    config_digest=self.config.digest,
                    layer_order=self.layer_ids,
                    completed_layers=quantization_stage["completed_layers"],
                    stages=copy_json(stages),
                    dependency={
                        "affected_layers": quantization_stage["affected_layers"],
                        "reused_layers": quantization_stage["reused_layers"],
                        "rebuilt_layers": quantization_stage["rebuilt_layers"],
                    },
                    result_digest=result_digest,
                    degradations=self._degradations,
                    injections=[
                        injection.as_dict() for injection in self.config.injections
                    ],
                    version_hint=(latest["version"] + 1) if latest else 1,
                    parent_checksum=latest.get("content_checksum") if latest else None,
                )
                stage_versions[STAGE_QUANTIZATION] = snapshot["version"]
                actions.append(
                    {
                        "stage": STAGE_QUANTIZATION,
                        "action": "commit",
                        "version": snapshot["version"],
                        "affected_layers": quantization_stage["affected_layers"],
                        "reused_layers": quantization_stage["reused_layers"],
                        "rebuilt_layers": quantization_stage["rebuilt_layers"],
                    }
                )

            calibration_reusable = self._stage_reusable(
                before,
                STAGE_CALIBRATION,
                self.manifest.digest,
                self.config.digest,
                self.layer_ids,
            )
            calibration_stage_affected = sorted(
                set(revision_affected) | set(calibration_affected),
                key=lambda layer_id: self.layer_index[layer_id],
            )
            if not previous_stage_data.get(STAGE_CALIBRATION):
                calibration_stage_affected = list(self.layer_ids)
            if calibration_reusable and not calibration_stage_affected:
                calibration_stage = copy_json(
                    previous_stage_data[STAGE_CALIBRATION]
                )
                stages[STAGE_CALIBRATION] = calibration_stage
                actions.append(
                    {
                        "stage": STAGE_CALIBRATION,
                        "action": "reuse",
                        "version": before.get("version"),
                    }
                )
            else:
                calibration_stage = self._compute_calibration_stage(
                    cache,
                    calibration_stage_affected,
                    grouped,
                    sample_digests,
                )
                stages[STAGE_CALIBRATION] = calibration_stage
                result_digest = self._result_digest(
                    self.manifest.digest, self.config.digest, stages
                )
                latest = self.store.load_latest()
                snapshot = self.store.commit(
                    stage=STAGE_CALIBRATION,
                    model_tag=self.manifest.model_tag,
                    manifest_digest=self.manifest.digest,
                    config_digest=self.config.digest,
                    layer_order=self.layer_ids,
                    completed_layers=calibration_stage["completed_layers"],
                    stages=copy_json(stages),
                    dependency={
                        "affected_layers": calibration_stage["affected_layers"],
                        "reused_layers": calibration_stage["reused_layers"],
                        "rebuilt_layers": calibration_stage["rebuilt_layers"],
                    },
                    result_digest=result_digest,
                    degradations=self._degradations,
                    injections=[
                        injection.as_dict() for injection in self.config.injections
                    ],
                    version_hint=(latest["version"] + 1) if latest else 1,
                    parent_checksum=latest.get("content_checksum") if latest else None,
                )
                stage_versions[STAGE_CALIBRATION] = snapshot["version"]
                actions.append(
                    {
                        "stage": STAGE_CALIBRATION,
                        "action": "commit",
                        "version": snapshot["version"],
                        "affected_layers": calibration_stage["affected_layers"],
                        "reused_layers": calibration_stage["reused_layers"],
                        "rebuilt_layers": calibration_stage["rebuilt_layers"],
                    }
                )

            inference_reusable = self._stage_reusable(
                before,
                STAGE_INFERENCE,
                self.manifest.digest,
                self.config.digest,
                self.layer_ids,
            )
            inference_affected = sorted(
                set(revision_affected) | set(calibration_affected),
                key=lambda layer_id: self.layer_index[layer_id],
            )
            if not previous_stage_data.get(STAGE_INFERENCE):
                inference_affected = list(self.layer_ids)
            initial_input = self._initial_input_for_inference()
            if inference_reusable and not inference_affected:
                inference_stage = copy_json(
                    previous_stage_data[STAGE_INFERENCE]
                )
                stages[STAGE_INFERENCE] = inference_stage
                diagnostics = inference_stage["diagnostics"]
                if not diagnostics.get("passed", False):
                    first = diagnostics["violations"][0]
                    raise BusinessError(
                        "E_TOLERANCE_EXCEEDED",
                        "layer serial error exceeds configured tolerance",
                        stage="accuracy_diagnostics",
                        layer_id=first["layer_id"],
                        layer_index=first["layer_index"],
                        details={"violations": diagnostics["violations"]},
                    )
                actions.append(
                    {
                        "stage": STAGE_INFERENCE,
                        "action": "reuse",
                        "version": before.get("version"),
                    }
                )
            else:
                committed_snapshot = self.store.load_latest()
                if not committed_snapshot:
                    raise BusinessError(
                        "E_LAYER_INCOMPLETE",
                        "no committed quantization snapshot is available",
                        stage=STAGE_INFERENCE,
                        layer_id=self.layer_ids[0],
                        layer_index=0,
                    )
                committed_quant_stage = committed_snapshot["stages"][
                    STAGE_QUANTIZATION
                ]
                inference_stage, diagnostics = self._compute_inference_stage(
                    cache,
                    inference_affected,
                    committed_quant_stage,
                    initial_input,
                )
                stages[STAGE_INFERENCE] = inference_stage
                result_digest = self._result_digest(
                    self.manifest.digest, self.config.digest, stages
                )
                latest = self.store.load_latest()
                snapshot = self.store.commit(
                    stage=STAGE_INFERENCE,
                    model_tag=self.manifest.model_tag,
                    manifest_digest=self.manifest.digest,
                    config_digest=self.config.digest,
                    layer_order=self.layer_ids,
                    completed_layers=inference_stage["completed_layers"],
                    stages=copy_json(stages),
                    dependency={
                        "affected_layers": inference_stage["affected_layers"],
                        "reused_layers": inference_stage["reused_layers"],
                        "rebuilt_layers": inference_stage["rebuilt_layers"],
                    },
                    result_digest=result_digest,
                    degradations=self._degradations,
                    injections=[
                        injection.as_dict() for injection in self.config.injections
                    ],
                    version_hint=(latest["version"] + 1) if latest else 1,
                    parent_checksum=latest.get("content_checksum") if latest else None,
                )
                stage_versions[STAGE_INFERENCE] = snapshot["version"]
                actions.append(
                    {
                        "stage": STAGE_INFERENCE,
                        "action": "commit",
                        "version": snapshot["version"],
                        "affected_layers": inference_stage["affected_layers"],
                        "reused_layers": inference_stage["reused_layers"],
                        "rebuilt_layers": inference_stage["rebuilt_layers"],
                    }
                )

            self.store.clear_inflight()
            final = self.store.load_latest()
            assert final is not None
            from .selfcheck import run_consistency_checks

            self_check = run_consistency_checks(final, self.config)
            report = {
                "ok": True,
                "model_tag": self.manifest.model_tag,
                "final_version": final["version"],
                "result_digest": final["result_digest"],
                "stage_versions": stage_versions,
                "actions": actions,
                "affected_layers": affected_union,
                "reused_layers": (
                    inference_stage.get("reused_layers", []) if "inference_stage" in locals() else []
                ),
                "rebuilt_layers": (
                    inference_stage.get("rebuilt_layers", []) if "inference_stage" in locals() else []
                ),
                "recovery": recovery,
                "degradations": final.get("degradations", []),
                "injections": final.get("injections", []),
                "summary": self._snapshot_summary(final),
                "self_check": self_check,
            }
            try:
                self.store.append_event(
                    {
                        "kind": "run_complete",
                        "version": final["version"],
                        "result_digest": final["result_digest"],
                        "actions": actions,
                        "affected_layers": affected_union,
                    }
                )
            except BusinessError:
                pass
            return report
        except BusinessError as exc:
            exc.push_context("engine:run")
            try:
                self.store.append_event(
                    {
                        "kind": "business_error",
                        "error": exc.as_dict(),
                    }
                )
            except BusinessError:
                pass
            raise

    def latest_snapshot(self) -> dict[str, Any]:
        snapshot = self.store.load_latest()
        if snapshot is None:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "no committed snapshot exists",
                stage="unknown",
            )
        return snapshot
