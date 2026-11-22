from __future__ import annotations

import copy
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .calibration import compute_calibration
from .config import DEFAULT_STAGE_NAMES, WorkbenchConfig
from .consistency import run_consistency
from .deterministic import generate_manifest, generate_weights, initial_activation
from .diagnostics import build_diagnostics
from .errors import (
    BusinessError,
    E_INVALID_CONFIG,
    E_MANIFEST_MISMATCH,
    E_NO_CHECKPOINT,
    E_RESOURCE_LIMIT,
    E_REVISION_ROLLBACK,
    E_SNAPSHOT_CORRUPT,
    attribute_stage,
)
from .inference import (
    NORMALIZATION_SCOPE,
    REDUCTION_ORDER,
    activated,
    blocked_matmul,
    resolve_block_policy,
)
from .manifest import (
    manifest_bytes,
    manifest_digest,
    parse_manifest_text,
    validate_manifest,
)
from .quantization import QuantizedLayer, quantize_layer
from .serialization import (
    array_checksum,
    atomic_write,
    atomic_write_json,
    canonical_json_text,
    content_checksum,
    decode_array,
    encode_array,
)
from .snapshots import Snapshot, SnapshotStore


STAGE_RANK = {stage: index for index, stage in enumerate(DEFAULT_STAGE_NAMES)}
WEIGHT_FIELDS = (
    "model_tag",
    "weight_revision",
    "layer_shape",
    "quant_dtype",
    "scale_hint",
    "zero_point",
    "block_policy",
)


class ForcedTermination(Exception):
    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__(f"forced termination after layer {info.get('layer_index')}")
        self.info = info


def _weight_signature(entry: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        canonical_json_text(entry[field]) if isinstance(entry[field], dict) else entry[field]
        for field in WEIGHT_FIELDS
    )


def _record_payload(
    records: dict[str, QuantizedLayer],
    entries: list[dict[str, Any]],
    *,
    total_weight_bytes: int,
) -> dict[str, Any]:
    ordered = [records[str(entry["layer_id"])].to_payload() for entry in entries if str(entry["layer_id"]) in records]
    return {
        "records": ordered,
        "manifest_entries": entries,
        "revisions": {
            str(entry["layer_id"]): int(entry["weight_revision"]) for entry in entries
        },
        "total_weight_bytes": int(total_weight_bytes),
        "rounding": "rint_half_even",
        "group_scaling_policy": "hint * (0.75 + 0.25 * group_abs_max / global_abs_max)",
    }


def _calibration_payload(
    stats: dict[str, dict[str, Any]],
    entries: list[dict[str, Any]],
    *,
    comparison_count: int,
    degradations: list[dict[str, Any]],
) -> dict[str, Any]:
    ordered = {
        str(entry["layer_id"]): stats[str(entry["layer_id"])]
        for entry in entries
        if str(entry["layer_id"]) in stats
    }
    return {
        "stats": ordered,
        "manifest_entries": entries,
        "comparison_count": int(comparison_count),
        "degradations": list(degradations),
        "comparison_unit": "used_batch * input_dim * output_dim",
    }


def _inference_payload(
    initial: np.ndarray,
    activations: dict[str, np.ndarray],
    policies: dict[str, dict[str, int | str]],
    entries: list[dict[str, Any]],
    *,
    degradations: list[dict[str, Any]],
    prediction: np.ndarray | None,
) -> dict[str, Any]:
    ordered_activations: dict[str, Any] = {}
    for entry in entries:
        layer_id = str(entry["layer_id"])
        if layer_id not in activations:
            break
        activation = activations[layer_id]
        ordered_activations[layer_id] = {
            "array": encode_array(activation),
            "checksum": array_checksum(activation),
            "shape": list(activation.shape),
        }
    payload: dict[str, Any] = {
        "initial_input": encode_array(initial),
        "initial_input_checksum": array_checksum(initial),
        "activations": ordered_activations,
        "policies": policies,
        "manifest_entries": entries,
        "reduction_order": REDUCTION_ORDER,
        "normalization_scope": NORMALIZATION_SCOPE,
        "degradations": list(degradations),
    }
    if prediction is not None:
        payload["final_prediction"] = encode_array(prediction)
        payload["final_prediction_checksum"] = array_checksum(prediction)
        payload["final_prediction_values"] = prediction.tolist()
    return payload


class Workbench:
    def __init__(self, config: WorkbenchConfig) -> None:
        self.config = config
        self.store = SnapshotStore(config.state_dir, config.temp_dir)
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.current_stage: str | None = None
        self.entries: list[dict[str, Any]] = []
        self.manifest_digest = ""
        self.prior: dict[str, Snapshot] = {}
        self.resume_latest: Snapshot | None = None
        self.is_resume = False
        self.is_invalidation = False
        self.recompute: dict[str, list[str]] = {stage: [] for stage in DEFAULT_STAGE_NAMES}
        self.reuse: dict[str, list[str]] = {stage: [] for stage in DEFAULT_STAGE_NAMES}
        self.invalidation: dict[str, Any] | None = None
        self.weight_affected_start: int | None = None
        self.calibration_affected_start: int | None = None
        self._last_commit: Snapshot | None = None
        self._initial: np.ndarray | None = None
        self._diagnostics: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def run(
        self,
        *,
        clean: bool = False,
        resume: bool = False,
        reuse_previous: bool = False,
        interrupt_after_inference_layer: int | None = None,
        termination_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        if clean and resume:
            raise BusinessError(E_INVALID_CONFIG, "clean 与 resume 不能同时启用", stage="config")
        if resume and reuse_previous:
            raise BusinessError(
                E_INVALID_CONFIG,
                "resume 与 reuse_previous 不能同时启用",
                stage="config",
            )
        if clean:
            self._clear_runtime()
        try:
            self._prepare_manifest()
            self._prepare_prior(resume=resume, reuse_previous=reuse_previous)
            if reuse_previous:
                self._detect_invalidation()
            self._run_validation()
            self._run_quantization()
            self._run_calibration()
            self._run_inference(
                interrupt_after_layer=interrupt_after_inference_layer,
                termination_hook=termination_hook,
            )
            result = self._finalize()
            return result
        except ForcedTermination:
            raise
        except BusinessError as exc:
            exc.with_context(stage=self.current_stage)
            self._record_error(exc)
            raise
        except Exception as exc:
            attributed = attribute_stage(self.current_stage or "pipeline", exc)
            self._record_error(attributed)
            raise attributed from exc

    # ------------------------------------------------------------------
    # Setup and bookkeeping
    # ------------------------------------------------------------------
    def _clear_runtime(self) -> None:
        self.store.clear()
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for child in self.config.temp_dir.glob("*"):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    def _prepare_manifest(self) -> None:
        self.current_stage = "model_validation"
        self.entries = generate_manifest(self.config)
        validate_manifest(self.entries)
        manifest_path = self.config.state_dir / "manifest.jsonl"
        atomic_write(
            manifest_path,
            manifest_bytes(self.entries),
            temp_dir=self.config.temp_dir,
        )
        parse_manifest_text(manifest_path.read_text(encoding="utf-8"))
        self.manifest_digest = manifest_digest(self.entries)
        self._initial = initial_activation(self.config, self.entries)

    def _prepare_prior(self, *, resume: bool, reuse_previous: bool) -> None:
        self.is_resume = resume
        self.is_invalidation = reuse_previous
        existing = self.store.latest(required=False)
        if not resume and not reuse_previous and existing is not None:
            raise BusinessError(
                E_INVALID_CONFIG,
                "状态目录已有进度，必须显式 clean、resume 或 reuse_previous",
                stage="config",
                details={"state_dir": str(self.config.state_dir)},
            )
        if resume and existing is None:
            raise BusinessError(
                E_NO_CHECKPOINT,
                "未找到已提交进度点，拒绝猜测续算位置",
                stage="resume",
                details={"state_dir": str(self.config.state_dir)},
            )
        if reuse_previous and existing is None:
            raise BusinessError(
                E_NO_CHECKPOINT,
                "失效重算需要已有已提交进度点",
                stage="invalidation",
                details={"state_dir": str(self.config.state_dir)},
            )
        if resume or reuse_previous:
            self.prior = self.store.all_stage_snapshots()
            self.resume_latest = self.store.latest(required=False)
        if resume:
            latest = self.resume_latest
            if latest is not None and latest.manifest_digest != self.manifest_digest:
                raise BusinessError(
                    E_MANIFEST_MISMATCH,
                    "当前清单摘要与已提交快照不一致，不能直接 resume",
                    stage="resume",
                    version=latest.version,
                    details={
                        "checkpoint_digest": latest.manifest_digest,
                        "current_digest": self.manifest_digest,
                    },
                )
            for stage, snapshot in self.prior.items():
                if snapshot.manifest_digest != self.manifest_digest:
                    raise BusinessError(
                        E_MANIFEST_MISMATCH,
                        f"阶段 {stage} 的快照清单摘要不一致",
                        stage=stage,
                        version=snapshot.version,
                    )

    def _record_error(self, error: BusinessError) -> None:
        payload = error.to_dict()
        payload["traceback"] = traceback.format_exc(limit=8)
        try:
            atomic_write_json(
                self.config.state_dir / "last_error.json",
                payload,
                temp_dir=self.config.temp_dir,
                indent=2,
            )
            log_path = self.config.state_dir / "error_log.jsonl"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json_text(payload) + "\n")
        except Exception:
            # Error reporting must never hide the original business error.
            pass

    def _event(
        self,
        *,
        stage: str,
        action: str,
        layer_id: str | None = None,
        version: int | None = None,
        detail: Any = None,
    ) -> None:
        self.events.append(
            {
                "stage": stage,
                "action": action,
                "layer_id": layer_id,
                "version": version,
                "detail": detail,
            }
        )

    def _enter_stage(self, stage: str) -> None:
        self.current_stage = stage
        if len(self.entries) > self.config.max_layers_per_stage:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "单阶段层数超过预算",
                stage=stage,
                details={
                    "layer_count": len(self.entries),
                    "max_layers_per_stage": self.config.max_layers_per_stage,
                },
            )

    def _prior_for(self, stage: str) -> Snapshot | None:
        return self.prior.get(stage)

    def _is_resume_partial_stage(self, stage: str, snapshot: Snapshot | None) -> bool:
        return bool(
            snapshot
            and self.resume_latest is not None
            and snapshot.version == self.resume_latest.version
            and snapshot.stage == stage
            and not snapshot.stage_complete
        )

    def _commit(
        self,
        stage: str,
        completed: list[str],
        payload: dict[str, Any],
        *,
        stage_complete: bool,
    ) -> Snapshot:
        snapshot = self.store.commit(
            stage=stage,
            completed_layers=completed,
            stage_complete=stage_complete,
            next_layer_index=len(completed),
            manifest_digest=self.manifest_digest,
            payload=payload,
        )
        self._last_commit = snapshot
        self._event(
            stage=stage,
            action="commit",
            version=snapshot.version,
            detail={
                "completed_layers": completed,
                "stage_complete": stage_complete,
            },
        )
        return snapshot

    # ------------------------------------------------------------------
    # Stage 1: manifest validation
    # ------------------------------------------------------------------
    def _run_validation(self) -> None:
        self._enter_stage("model_validation")
        prior = self._prior_for("model_validation")
        payload: dict[str, Any] = {}
        completed: list[str] = []
        layers: dict[str, Any] = {}
        reuse_prefix = False
        if prior and prior.manifest_digest == self.manifest_digest:
            layers = dict(prior.payload.get("layers", {}))
            completed = list(prior.completed_layers)
            if prior.stage_complete or not self._is_resume_partial_stage(
                "model_validation", prior
            ):
                completed = [str(entry["layer_id"]) for entry in self.entries]
            else:
                reuse_prefix = True
            if prior.stage_complete:
                for layer_id in completed:
                    self.reuse["model_validation"].append(layer_id)
                    self._event(
                        stage="model_validation",
                        action="reuse_layer",
                        layer_id=layer_id,
                        version=prior.version,
                    )
        if reuse_prefix and not prior:
            reuse_prefix = False
        start_index = len(completed)
        max_layers = len(self.entries)
        if start_index > max_layers:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "模型校验快照的已完成层数越界",
                stage="model_validation",
                version=prior.version if prior else None,
            )
        for entry in self.entries[start_index:]:
            layer_id = str(entry["layer_id"])
            layers[layer_id] = {
                "manifest": copy.deepcopy(entry),
                "checksum": content_checksum(entry),
            }
            completed.append(layer_id)
            self.recompute["model_validation"].append(layer_id)
            self._event(
                stage="model_validation",
                action="compute_layer",
                layer_id=layer_id,
            )
            payload = {
                "layers": layers,
                "manifest_digest": self.manifest_digest,
                "model_tag": str(self.entries[0]["model_tag"]),
            }
            self._commit(
                "model_validation",
                completed,
                payload,
                stage_complete=len(completed) == len(self.entries),
            )
        if start_index == len(self.entries) and not completed:
            completed = [str(entry["layer_id"]) for entry in self.entries]
            layers = {
                str(entry["layer_id"]): {
                    "manifest": copy.deepcopy(entry),
                    "checksum": content_checksum(entry),
                }
                for entry in self.entries
            }
            payload = {
                "layers": layers,
                "manifest_digest": self.manifest_digest,
                "model_tag": str(self.entries[0]["model_tag"]),
            }
            self._commit(
                "model_validation",
                completed,
                payload,
                stage_complete=True,
            )

    # ------------------------------------------------------------------
    # Stage 2: deterministic quantization
    # ------------------------------------------------------------------
    def _load_quant_records(self, snapshot: Snapshot) -> dict[str, QuantizedLayer]:
        return {
            str(payload["layer_id"]): QuantizedLayer.from_payload(payload)
            for payload in snapshot.payload.get("records", [])
        }

    def _run_quantization(self) -> None:
        self._enter_stage("quantization")
        prior = self._prior_for("quantization")
        records: dict[str, QuantizedLayer] = {}
        completed: list[str] = []
        total_weight_bytes = 0
        commit_each = True
        final_commit_needed = False
        start_index = 0
        if prior and prior.manifest_digest == self.manifest_digest:
            records = self._load_quant_records(prior)
            completed = list(prior.completed_layers)
            total_weight_bytes = int(prior.payload.get("total_weight_bytes", 0))
            start_index = len(completed)
            commit_each = not prior.stage_complete
            for layer_id in completed:
                self.reuse["quantization"].append(layer_id)
                self._event(
                    stage="quantization",
                    action="reuse_layer",
                    layer_id=layer_id,
                    version=prior.version,
                )
        elif prior and self.is_resume and prior.manifest_digest != self.manifest_digest:
            raise BusinessError(
                E_MANIFEST_MISMATCH,
                "量化快照与当前清单不匹配",
                stage="quantization",
                version=prior.version,
            )
        elif prior and self.invalidation is not None:
            old_records = self._load_quant_records(prior)
            old_completed = list(prior.completed_layers)
            recommended_start = (
                int(self.weight_affected_start)
                if self.weight_affected_start is not None
                else len(self.entries)
            )
            start_index = min(recommended_start, len(old_completed))
            for entry in self.entries[:start_index]:
                layer_id = str(entry["layer_id"])
                old = old_records[layer_id]
                if old.weight_revision != int(entry["weight_revision"]):
                    raise BusinessError(
                        E_REVISION_ROLLBACK,
                        "上游复用层版本与清单不一致",
                        stage="quantization",
                        layer_id=layer_id,
                        layer_index=int(entry["layer_index"]),
                    )
                records[layer_id] = old
                completed.append(layer_id)
                total_weight_bytes += old.weight_bytes
                self.reuse["quantization"].append(layer_id)
            commit_each = False
            final_commit_needed = True
        for entry in self.entries[start_index:]:
            layer_id = str(entry["layer_id"])
            layer_index = int(entry["layer_index"])
            weights = generate_weights(self.config, entry)
            record = quantize_layer(weights, entry)
            projected = total_weight_bytes + record.weight_bytes
            if projected > self.config.max_resident_weight_bytes:
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "驻留反量化权重内存超过预算",
                    stage="quantization",
                    layer_id=layer_id,
                    layer_index=layer_index,
                    details={
                        "projected_bytes": projected,
                        "max_resident_weight_bytes": self.config.max_resident_weight_bytes,
                    },
                )
            total_weight_bytes = projected
            records[layer_id] = record
            completed.append(layer_id)
            self.recompute["quantization"].append(layer_id)
            self._event(
                stage="quantization",
                action="compute_layer",
                layer_id=layer_id,
                detail={
                    "dequantized_checksum": record.dequantized_checksum,
                    "weight_bytes": record.weight_bytes,
                },
            )
            payload = _record_payload(
                records,
                self.entries,
                total_weight_bytes=total_weight_bytes,
            )
            if commit_each:
                self._commit(
                    "quantization",
                    completed,
                    payload,
                    stage_complete=len(completed) == len(self.entries),
                )
        payload = _record_payload(
            records,
            self.entries,
            total_weight_bytes=total_weight_bytes,
        )
        if final_commit_needed:
            self._commit(
                "quantization",
                completed,
                payload,
                stage_complete=True,
            )
        final_prior = self._prior_for("quantization")
        if final_prior and final_prior.stage_complete and not payload.get("records"):
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "量化阶段没有可用快照",
                stage="quantization",
                version=final_prior.version,
            )
        # Verify the committed view, not only in-memory records.
        committed = self.store.stage_snapshot("quantization", required=True)
        if committed is None or not committed.stage_complete:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "量化阶段未完整提交",
                stage="quantization",
                version=committed.version if committed else None,
            )
        committed_records = self._load_quant_records(committed)
        if set(committed_records) != {str(entry["layer_id"]) for entry in self.entries}:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "量化提交层集合不完整",
                stage="quantization",
                version=committed.version,
            )
        self._quant_snapshot = committed
        self._quant_records = committed_records

    # ------------------------------------------------------------------
    # Stage 3: calibration
    # ------------------------------------------------------------------
    def _run_calibration(self) -> None:
        self._enter_stage("calibration")
        quant_snapshot = self.store.stage_snapshot("quantization", required=True)
        if quant_snapshot is None or not quant_snapshot.stage_complete:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "校准阶段要求完整的已提交量化快照",
                stage="calibration",
            )
        records = self._load_quant_records(quant_snapshot)
        prior = self._prior_for("calibration")
        stats: dict[str, dict[str, Any]] = {}
        completed: list[str] = []
        comparison_count = 0
        degradations: list[dict[str, Any]] = []
        start_index = 0
        commit_each = True
        final_commit_needed = False
        if prior and prior.manifest_digest == self.manifest_digest:
            stats = dict(prior.payload.get("stats", {}))
            completed = list(prior.completed_layers)
            comparison_count = int(prior.payload.get("comparison_count", 0))
            degradations = list(prior.payload.get("degradations", []))
            start_index = len(completed)
            commit_each = not prior.stage_complete
            for layer_id in completed:
                self.reuse["calibration"].append(layer_id)
                self._event(
                    stage="calibration",
                    action="reuse_layer",
                    layer_id=layer_id,
                    version=prior.version,
                )
        elif prior and self.invalidation is not None:
            old_stats = dict(prior.payload.get("stats", {}))
            old_completed = list(prior.completed_layers)
            recommended_start = (
                int(self.calibration_affected_start)
                if self.calibration_affected_start is not None
                else len(self.entries)
            )
            start_index = min(recommended_start, len(old_completed))
            for entry in self.entries[:start_index]:
                layer_id = str(entry["layer_id"])
                stats[layer_id] = old_stats[layer_id]
                completed.append(layer_id)
                comparison_count += int(old_stats[layer_id]["comparison_count"])
                self.reuse["calibration"].append(layer_id)
            degradations = [
                item
                for item in prior.payload.get("degradations", [])
                if int(item.get("layer_index", 0)) < start_index
            ]
            commit_each = False
            final_commit_needed = True
        for entry in self.entries[start_index:]:
            layer_id = str(entry["layer_id"])
            layer_index = int(entry["layer_index"])
            remaining = self.config.max_calibration_comparisons - comparison_count
            stats_payload, degradation = compute_calibration(
                self.config,
                entry,
                records[layer_id],
                remaining_comparisons=remaining,
            )
            if degradation:
                degradations.append(degradation)
            comparison_count += int(stats_payload["comparison_count"])
            stats[layer_id] = stats_payload
            completed.append(layer_id)
            self.recompute["calibration"].append(layer_id)
            self._event(
                stage="calibration",
                action="compute_layer",
                layer_id=layer_id,
                detail={"comparison_count": stats_payload["comparison_count"]},
            )
            payload = _calibration_payload(
                stats,
                self.entries,
                comparison_count=comparison_count,
                degradations=degradations,
            )
            if commit_each:
                self._commit(
                    "calibration",
                    completed,
                    payload,
                    stage_complete=len(completed) == len(self.entries),
                )
        payload = _calibration_payload(
            stats,
            self.entries,
            comparison_count=comparison_count,
            degradations=degradations,
        )
        if final_commit_needed:
            self._commit(
                "calibration",
                completed,
                payload,
                stage_complete=True,
            )
        committed = self.store.stage_snapshot("calibration", required=True)
        if committed is None or not committed.stage_complete:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "校准阶段未完整提交",
                stage="calibration",
                version=committed.version if committed else None,
            )
        self._calibration_snapshot = committed

    # ------------------------------------------------------------------
    # Stage 4: blocked inference
    # ------------------------------------------------------------------
    def _run_inference(
        self,
        *,
        interrupt_after_layer: int | None,
        termination_hook: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self._enter_stage("inference")
        quant_snapshot = self.store.stage_snapshot("quantization", required=True)
        if quant_snapshot is None or not quant_snapshot.stage_complete:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "推理阶段要求完整的已提交量化快照",
                stage="inference",
            )
        records = self._load_quant_records(quant_snapshot)
        initial = self._initial
        assert initial is not None
        prior = self._prior_for("inference")
        activations: dict[str, np.ndarray] = {}
        policies: dict[str, dict[str, int | str]] = {}
        degradations: list[dict[str, Any]] = []
        completed: list[str] = []
        start_index = 0
        commit_each = True
        final_commit_needed = False
        prediction: np.ndarray | None = None
        if prior and prior.manifest_digest == self.manifest_digest:
            prior_initial = decode_array(prior.payload["initial_input"])
            if not np.array_equal(prior_initial, initial):
                raise BusinessError(
                    E_MANIFEST_MISMATCH,
                    "推理输入与已提交快照不一致",
                    stage="inference",
                    version=prior.version,
                )
            for layer_id, item in prior.payload.get("activations", {}).items():
                activations[layer_id] = decode_array(item["array"])
            policies.update(prior.payload.get("policies", {}))
            degradations.extend(prior.payload.get("degradations", []))
            completed = list(prior.completed_layers)
            start_index = len(completed)
            commit_each = not prior.stage_complete
            for layer_id in completed:
                self.reuse["inference"].append(layer_id)
                self._event(
                    stage="inference",
                    action="reuse_layer",
                    layer_id=layer_id,
                    version=prior.version,
                )
            if "final_prediction" in prior.payload:
                prediction = decode_array(prior.payload["final_prediction"])
        elif prior and self.invalidation is not None:
            old_initial = decode_array(prior.payload["initial_input"])
            if not np.array_equal(old_initial, initial):
                raise BusinessError(
                    E_MANIFEST_MISMATCH,
                    "失效重算时初始激活不一致",
                    stage="inference",
                    version=prior.version,
                )
            old_completed = list(prior.completed_layers)
            recommended_start = (
                int(self.calibration_affected_start)
                if self.calibration_affected_start is not None
                else len(self.entries)
            )
            start_index = min(recommended_start, len(old_completed))
            for entry in self.entries[:start_index]:
                layer_id = str(entry["layer_id"])
                activations[layer_id] = decode_array(
                    prior.payload["activations"][layer_id]["array"]
                )
                completed.append(layer_id)
                if layer_id in prior.payload.get("policies", {}):
                    policies[layer_id] = prior.payload["policies"][layer_id]
                self.reuse["inference"].append(layer_id)
            degradations.extend(
                item
                for item in prior.payload.get("degradations", [])
                if int(item.get("layer_index", 0)) < start_index
            )
            if start_index == len(self.entries) and "final_prediction" in prior.payload:
                prediction = decode_array(prior.payload["final_prediction"])
            commit_each = False
            final_commit_needed = True

        if start_index > 0 and start_index < len(self.entries):
            previous_id = str(self.entries[start_index - 1]["layer_id"])
            x = activations[previous_id]
        else:
            x = initial
        for entry in self.entries[start_index:]:
            layer_id = str(entry["layer_id"])
            layer_index = int(entry["layer_index"])
            policy, degradation = resolve_block_policy(
                entry,
                batch_size=int(x.shape[0]),
                max_block_elements=self.config.max_block_elements,
            )
            if degradation:
                degradations.append(degradation)
            linear = blocked_matmul(x, records[layer_id].dequantized, policy)
            x = activated(linear)
            activations[layer_id] = x
            policies[layer_id] = policy
            completed.append(layer_id)
            self.recompute["inference"].append(layer_id)
            self._event(
                stage="inference",
                action="compute_layer",
                layer_id=layer_id,
                detail={"activation_checksum": array_checksum(x)},
            )
            prediction = (
                np.ascontiguousarray(np.mean(x, axis=0))
                if layer_index == len(self.entries) - 1
                else None
            )
            payload = _inference_payload(
                initial,
                activations,
                policies,
                self.entries,
                degradations=degradations,
                prediction=prediction,
            )
            if commit_each:
                snapshot = self._commit(
                    "inference",
                    completed,
                    payload,
                    stage_complete=len(completed) == len(self.entries),
                )
                if interrupt_after_layer is not None and layer_index == interrupt_after_layer:
                    info = {
                        "stage": "inference",
                        "layer_index": layer_index,
                        "layer_id": layer_id,
                        "version": snapshot.version,
                        "completed_layers": list(completed),
                    }
                    if termination_hook is not None:
                        termination_hook(info)
                    else:
                        raise ForcedTermination(info)
        if prediction is None:
            final_id = str(self.entries[-1]["layer_id"])
            prediction = np.ascontiguousarray(np.mean(activations[final_id], axis=0))
        payload = _inference_payload(
            initial,
            activations,
            policies,
            self.entries,
            degradations=degradations,
            prediction=prediction,
        )
        if final_commit_needed:
            self._commit(
                "inference",
                completed,
                payload,
                stage_complete=True,
            )
        committed = self.store.stage_snapshot("inference", required=True)
        if committed is None or not committed.stage_complete:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "推理阶段未完整提交",
                stage="inference",
                version=committed.version if committed else None,
            )
        self._inference_snapshot = committed
        self._activations = activations
        self._prediction = prediction

    # ------------------------------------------------------------------
    # Invalidation dependency analysis
    # ------------------------------------------------------------------
    def _detect_invalidation(self) -> None:
        self.current_stage = "invalidation"
        quant = self.prior.get("quantization")
        if quant is None:
            raise BusinessError(
                E_NO_CHECKPOINT,
                "缺少量化阶段进度，无法计算失效层集合",
                stage="invalidation",
            )
        old_entries = {
            int(entry["layer_index"]): entry
            for entry in quant.payload.get("manifest_entries", [])
        }
        weight_changed: list[int] = []
        calibration_changed: list[int] = []
        for entry in self.entries:
            index = int(entry["layer_index"])
            old = old_entries.get(index)
            if old is None:
                weight_changed.append(index)
                calibration_changed.append(index)
                continue
            old_revision = int(old.get("weight_revision", 0))
            new_revision = int(entry["weight_revision"])
            if new_revision < old_revision:
                raise BusinessError(
                    E_REVISION_ROLLBACK,
                    "weight_revision 回退被拒绝",
                    stage="invalidation",
                    layer_id=str(entry["layer_id"]),
                    layer_index=index,
                    details={"old": old_revision, "new": new_revision},
                )
            if _weight_signature(old) != _weight_signature(entry):
                weight_changed.append(index)
            if int(old.get("calib_seed", -1)) != int(entry["calib_seed"]):
                calibration_changed.append(index)
        self.weight_affected_start = min(weight_changed) if weight_changed else None
        calibration_start_candidates = [
            value
            for value in (self.weight_affected_start, min(calibration_changed) if calibration_changed else None)
            if value is not None
        ]
        self.calibration_affected_start = (
            min(calibration_start_candidates) if calibration_start_candidates else None
        )
        affected_start = self.calibration_affected_start
        affected = (
            [str(entry["layer_id"]) for entry in self.entries[int(affected_start):]]
            if affected_start is not None
            else []
        )
        reused = [
            str(entry["layer_id"])
            for entry in self.entries[: int(affected_start)]
        ] if affected_start is not None else [str(entry["layer_id"]) for entry in self.entries]
        self.invalidation = {
            "mode": "dependency_invalidation",
            "triggers": {
                "weight_revision": bool(weight_changed),
                "calibration": bool(calibration_changed),
            },
            "weight_changed_layers": [
                str(self.entries[index]["layer_id"]) for index in weight_changed
            ],
            "calibration_changed_layers": [
                str(self.entries[index]["layer_id"]) for index in calibration_changed
            ],
            "affected_layers": affected,
            "recomputed_layers": affected,
            "reused_layers": reused,
            "disjoint": set(affected).isdisjoint(reused),
            "union_equals_all_layers": set(affected) | set(reused) == {
                str(entry["layer_id"]) for entry in self.entries
            },
            "covers_affected": set(affected).issubset(set(affected) | set(reused)),
            "by_stage": {},
        }

    # ------------------------------------------------------------------
    # Finalization and persistence
    # ------------------------------------------------------------------
    def _budget_report(self) -> dict[str, Any]:
        quant_records = getattr(self, "_quant_records", {})
        calibration = self.store.stage_snapshot("calibration")
        inference = self.store.stage_snapshot("inference")
        degradations: list[dict[str, Any]] = []
        if calibration:
            degradations.extend(calibration.payload.get("degradations", []))
        if inference:
            degradations.extend(inference.payload.get("degradations", []))
        seen: set[str] = set()
        unique_degradations: list[dict[str, Any]] = []
        for item in degradations:
            key = canonical_json_text(item)
            if key not in seen:
                seen.add(key)
                unique_degradations.append(item)
        usage = {
            "layer_count": len(self.entries),
            "resident_dequantized_weight_bytes": int(
                sum(record.weight_bytes for record in quant_records.values())
            ),
            "calibration_comparisons": int(
                calibration.payload.get("comparison_count", 0) if calibration else 0
            ),
            "block_element_limit": int(self.config.max_block_elements),
        }
        return {
            "limits": {
                "max_layers_per_stage": int(self.config.max_layers_per_stage),
                "max_resident_weight_bytes": int(self.config.max_resident_weight_bytes),
                "max_calibration_comparisons": int(
                    self.config.max_calibration_comparisons
                ),
                "max_block_elements": int(self.config.max_block_elements),
            },
            "usage": usage,
            "degradations": unique_degradations,
            "degradation_categories": sorted(
                {str(item.get("category")) for item in unique_degradations}
            ),
        }

    def _execution_report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "events": self.events,
            "recomputed_layers_by_stage": self.recompute,
            "reused_layers_by_stage": self.reuse,
        }
        if self.invalidation is not None:
            invalidation = copy.deepcopy(self.invalidation)
            invalidation["by_stage"] = {
                stage: {
                    "recomputed_layers": self.recompute[stage],
                    "reused_layers": self.reuse[stage],
                    "disjoint": set(self.recompute[stage]).isdisjoint(self.reuse[stage]),
                }
                for stage in DEFAULT_STAGE_NAMES
            }
            computation_stages = ("quantization", "calibration", "inference")
            recomputed = {
                layer_id
                for stage in computation_stages
                for layer_id in self.recompute[stage]
            }
            reused_sets = [set(self.reuse[stage]) for stage in computation_stages]
            reused = set.intersection(*reused_sets) if reused_sets else set()
            invalidation["recomputed_layers"] = [
                str(entry["layer_id"])
                for entry in self.entries
                if str(entry["layer_id"]) in recomputed
            ]
            invalidation["reused_layers"] = [
                str(entry["layer_id"])
                for entry in self.entries
                if str(entry["layer_id"]) in reused
            ]
            invalidation["disjoint"] = recomputed.isdisjoint(reused)
            invalidation["union_equals_all_layers"] = (
                recomputed | reused
                == {str(entry["layer_id"]) for entry in self.entries}
            )
            invalidation["covers_affected"] = set(
                invalidation["affected_layers"]
            ).issubset(recomputed | reused)
            self.invalidation = invalidation
            report["invalidation"] = invalidation
        return report

    def _finalize(self) -> dict[str, Any]:
        self.current_stage = "diagnostics"
        quant_snapshot = self.store.stage_snapshot("quantization", required=True)
        assert quant_snapshot is not None
        records = self._load_quant_records(quant_snapshot)
        assert self._initial is not None and self._prediction is not None
        diagnostics = build_diagnostics(
            self.config,
            self.entries,
            records,
            initial=self._initial,
            operational_activations=self._activations,
            operational_prediction=self._prediction,
        )
        self._diagnostics = diagnostics
        execution = self._execution_report()
        consistency = run_consistency(
            self.config,
            self.store,
            self.entries,
            invalidation=execution.get("invalidation"),
        )
        progress = self.store.progress_view()
        calibration_snapshot = self.store.stage_snapshot("calibration", required=True)
        inference_snapshot = self.store.stage_snapshot("inference", required=True)
        assert calibration_snapshot and inference_snapshot
        quant_summaries = []
        for entry in self.entries:
            record = records[str(entry["layer_id"])]
            quant_summaries.append(
                {
                    "layer_id": record.layer_id,
                    "layer_index": record.layer_index,
                    "layer_shape": list(record.layer_shape),
                    "weight_revision": record.weight_revision,
                    "quant_dtype": record.quant_dtype,
                    "zero_point": record.zero_point,
                    "group_size": record.group_size,
                    "scale_min": float(np.min(record.scales)),
                    "scale_max": float(np.max(record.scales)),
                    "scale_count": int(record.scales.size),
                    "scales": record.scales.tolist(),
                    "dequantized_checksum": record.dequantized_checksum,
                    "original_checksum": record.original_checksum,
                    "weight_bytes": record.weight_bytes,
                    "error": record.error,
                }
            )
        calibration_summaries = []
        for entry in self.entries:
            stats = calibration_snapshot.payload["stats"][str(entry["layer_id"])]
            calibration_summaries.append(
                {
                    key: value
                    for key, value in stats.items()
                    if key not in {"samples", "outputs"}
                }
            )
        inference_layers = []
        for entry in self.entries:
            layer_id = str(entry["layer_id"])
            item = inference_snapshot.payload["activations"][layer_id]
            inference_layers.append(
                {
                    "layer_id": layer_id,
                    "layer_index": int(entry["layer_index"]),
                    "activation_checksum": item["checksum"],
                    "shape": item["shape"],
                    "block_policy": inference_snapshot.payload["policies"].get(layer_id, {}),
                }
            )
        recovery = {
            "mode": (
                "resume"
                if self.is_resume
                else ("invalidation" if self.is_invalidation else "fresh")
            ),
            "resume_from": self.resume_latest.summary() if self.resume_latest else None,
            "reused_layers_by_stage": self.reuse,
            "recomputed_layers_by_stage": self.recompute,
            "checkpoint_versions": {
                stage: snapshot.summary()
                for stage, snapshot in self.store.all_stage_snapshots().items()
            },
        }
        result: dict[str, Any] = {
            "schema": 1,
            "model_tag": str(self.entries[0]["model_tag"]),
            "manifest_digest": self.manifest_digest,
            "config": self.config.to_dict(),
            "manifest": self.entries,
            "initial_input_checksum": array_checksum(self._initial),
            "quantization": {
                "rounding": "rint_half_even",
                "layers": quant_summaries,
                "total_weight_bytes": int(
                    sum(record.weight_bytes for record in records.values())
                ),
                "snapshot": quant_snapshot.summary(),
            },
            "calibration": {
                "layers": calibration_summaries,
                "comparison_count": int(
                    calibration_snapshot.payload.get("comparison_count", 0)
                ),
                "snapshot": calibration_snapshot.summary(),
            },
            "inference": {
                "layers": inference_layers,
                "final_prediction": inference_snapshot.payload["final_prediction_values"],
                "final_prediction_checksum": inference_snapshot.payload[
                    "final_prediction_checksum"
                ],
                "reduction_order": inference_snapshot.payload["reduction_order"],
                "normalization_scope": inference_snapshot.payload[
                    "normalization_scope"
                ],
                "snapshot": inference_snapshot.summary(),
            },
            "diagnostics": diagnostics,
            "execution": execution,
            "recovery": recovery,
            "progress": progress,
            "budget": self._budget_report(),
            "consistency": consistency,
            "recent_errors": self._recent_errors(),
        }
        self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.config.artifacts_dir / "result.json",
            result,
            temp_dir=self.config.temp_dir,
            indent=2,
        )
        from .dashboard import build_dashboard

        dashboard = build_dashboard(result, result_path=self.config.artifacts_dir / "result.json")
        atomic_write_json(
            self.config.artifacts_dir / "dashboard.json",
            dashboard,
            temp_dir=self.config.temp_dir,
            indent=2,
        )
        return result

    def _recent_errors(self) -> list[dict[str, Any]]:
        path = self.config.state_dir / "error_log.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()[-5:]
        import json

        values = []
        for line in lines:
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return values
