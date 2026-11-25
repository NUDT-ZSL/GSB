"""Pseudo-label incremental calibration and drift-failure propagation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import StageBusyError
from .execution_control import BudgetConfig, CalibrationGate, admit_calibration_batch
from .snapshots import SnapshotStore
from .storage import MetadataStore
from .threshold_formats import write_v2
from .thresholds import (
    EFFECTIVE_TIER,
    GLOBAL_SEGMENT,
    ThresholdModel,
    ThresholdRow,
    build_rows_from_samples,
)


@dataclass
class CalibrationRequest:
    reason: str
    feature_index: dict[str, dict[str, Any]]
    labels: dict[str, int] | None = None
    polluted_labels: dict[str, int] | None = None
    window_updates: list[dict[str, Any]] = field(default_factory=list)
    requested_segments: list[str] = field(default_factory=list)
    drift_alert: dict[str, Any] | None = None
    batch_id: int | None = None
    input_dim: int = 128
    pinned_window_ids: list[str] = field(default_factory=list)


class CalibrationService:
    def __init__(
        self,
        state_dir: str | Path,
        metadata: MetadataStore | None = None,
        snapshots: SnapshotStore | None = None,
        budget_config: BudgetConfig | None = None,
        gate: CalibrationGate | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.metadata = metadata or MetadataStore(self.state_dir)
        self.snapshots = snapshots or SnapshotStore(self.state_dir)
        self.budget_config = budget_config or BudgetConfig()
        self.gate = gate or CalibrationGate(self.state_dir)

    def submit(self, model: ThresholdModel, request: CalibrationRequest | dict) -> dict[str, Any]:
        if isinstance(request, dict):
            request = CalibrationRequest(**request)

        feature_index = dict(request.feature_index)
        changed_ids = self._changed_window_ids(request)
        affected_segments = self._affected_segments(model, request, feature_index, changed_ids)
        candidate_ids = [
            window_id
            for window_id, item in feature_index.items()
            if item.get("segment_id") in affected_segments
        ]
        budget = admit_calibration_batch(
            candidate_ids,
            request.input_dim,
            self.budget_config,
            pinned_window_ids=list(set(changed_ids + request.pinned_window_ids)),
        )
        budget_dict = budget.to_dict()
        if not budget.admitted:
            self.metadata.record_calibration(
                {
                    "run_id": f"cal-v{model.model_version + 1}-rejected",
                    "batch_id": request.batch_id,
                    "reason": request.reason,
                    "status": "rejected",
                    "old_model_version": model.model_version,
                    "error_code": "E_BUDGET_EXCEEDED",
                    "budget": budget_dict,
                }
            )
            return {"ok": False, "error_code": "E_BUDGET_EXCEEDED", "budget": budget_dict}

        if not self.gate.try_acquire():
            busy = StageBusyError(
                "another threshold calibration commit is active",
                details={"error_code": StageBusyError.code},
            ).to_outcome()
            busy["budget"] = budget_dict
            self.metadata.record_calibration(
                {
                    "run_id": f"cal-v{model.model_version + 1}-busy",
                    "batch_id": request.batch_id,
                    "reason": request.reason,
                    "status": "busy",
                    "old_model_version": model.model_version,
                    "error_code": StageBusyError.code,
                    "budget": budget_dict,
                }
            )
            return busy

        try:
            labels = dict(request.labels or self.metadata.effective_labels())
            for update in request.window_updates:
                window_id = str(update["window_id"])
                label_text = update.get("corrected_label")
                if label_text is not None:
                    labels[window_id] = self.metadata.correct_label(window_id, str(label_text))
                elif "label" in update:
                    label = int(update["label"])
                    labels[window_id] = label
                    self.metadata.correct_label(window_id, "anomaly" if label == 1 else "normal")

            selected_set = set(budget.selected_window_ids)
            new_model = self._recompute_model(
                model=model,
                request=request,
                affected_segments=affected_segments,
                feature_index=feature_index,
                labels=labels,
                selected_set=selected_set,
                budget_degraded=budget.degraded,
            )
            propagation = self._propagation_plan(
                model=model,
                new_model=new_model,
                affected_segments=affected_segments,
                feature_index=feature_index,
                request=request,
                changed_ids=changed_ids,
            )
            new_model.calibration_path.append(
                f"calibration:v{new_model.model_version}:{request.reason}:"
                + ",".join(affected_segments or [GLOBAL_SEGMENT])
            )
            new_model.recompute_checksum()

            snapshot = self.snapshots.commit("threshold", new_model.to_dict())
            v2_dir = self.state_dir / "threshold_model_v2"
            write_v2(v2_dir, new_model)

            run_id = f"cal-v{new_model.model_version}-{request.reason}"
            self.metadata.record_calibration(
                {
                    "run_id": run_id,
                    "batch_id": request.batch_id,
                    "reason": request.reason,
                    "status": "committed",
                    "old_model_version": model.model_version,
                    "new_model_version": new_model.model_version,
                    "budget": budget_dict,
                    "propagation": propagation,
                }
            )
            return {
                "ok": True,
                "error_code": None,
                "model": new_model,
                "snapshot": snapshot,
                "budget": budget_dict,
                "propagation": propagation,
                "threshold_v2_dir": str(v2_dir),
            }
        finally:
            self.gate.release()

    def _changed_window_ids(self, request: CalibrationRequest) -> list[str]:
        ids = [str(item["window_id"]) for item in request.window_updates if "window_id" in item]
        ids.extend(request.pinned_window_ids)
        return sorted(set(ids))

    def _affected_segments(
        self,
        model: ThresholdModel,
        request: CalibrationRequest,
        feature_index: dict[str, dict[str, Any]],
        changed_ids: list[str],
    ) -> list[str]:
        affected = set(request.requested_segments)
        if request.drift_alert:
            affected.update(request.drift_alert.get("affected_segments", []))
        for window_id in changed_ids:
            item = feature_index.get(window_id)
            if item and item.get("segment_id"):
                affected.add(str(item["segment_id"]))
        return sorted(affected)

    def _recompute_model(
        self,
        model: ThresholdModel,
        request: CalibrationRequest,
        affected_segments: list[str],
        feature_index: dict[str, dict[str, Any]],
        labels: dict[str, int],
        selected_set: set[str],
        budget_degraded: bool,
    ) -> ThresholdModel:
        new_model = ThresholdModel.from_dict(model.to_dict())
        new_model.model_version = int(model.model_version) + 1
        new_model.migration_sources = list(model.migration_sources)

        affected_set = set(affected_segments)
        replaced_keys = {(segment, "score", tier) for segment in affected_set for tier in (EFFECTIVE_TIER, "pseudo", "verified")}
        new_model.rows = [
            row
            for row in new_model.rows
            if (row.segment_id, row.metric, row.tier) not in replaced_keys
        ]

        for segment in affected_segments:
            segment_ids = [
                window_id
                for window_id, item in feature_index.items()
                if item.get("segment_id") == segment and window_id in selected_set
            ]
            normal_ids = [window_id for window_id in segment_ids if int(labels.get(window_id, 0)) == 0]
            effective_values = [float(feature_index[window_id]["score"]) for window_id in normal_ids]
            if effective_values:
                rows = build_rows_from_samples({segment: effective_values})
                effective_row = rows[0]
                effective_row.tier = EFFECTIVE_TIER
                effective_row.granularity = model.default_granularity
                effective_row.sample_count = len(effective_values)
                if budget_degraded:
                    effective_row.condition = {"budget_degraded": True}
                effective_row.recompute_checksum()
                new_model.rows.append(effective_row)

            polluted = request.polluted_labels or {}
            pseudo_values = [
                float(feature_index[window_id]["score"])
                for window_id in segment_ids
                if window_id in polluted and int(polluted[window_id]) == 0
                and window_id not in labels
            ]
            if pseudo_values:
                pseudo_row = build_rows_from_samples({segment: pseudo_values})[0]
                pseudo_row.tier = "pseudo"
                pseudo_row.recompute_checksum()
                new_model.rows.append(pseudo_row)

            verified_values = [
                float(feature_index[window_id]["score"])
                for window_id in segment_ids
                if request.window_updates
                and any(u.get("window_id") == window_id and u.get("corrected_label") == "normal" for u in request.window_updates)
            ]
            if verified_values:
                verified_row = build_rows_from_samples({segment: verified_values})[0]
                verified_row.tier = "verified"
                verified_row.condition = {"label_source": "pseudo_label_correction"}
                verified_row.recompute_checksum()
                new_model.rows.append(verified_row)

        all_normal_values = [
            float(item["score"])
            for window_id, item in feature_index.items()
            if int(labels.get(window_id, 0)) == 0 and (not selected_set or window_id in selected_set or not budget_degraded)
        ]
        if all_normal_values:
            global_rows = build_rows_from_samples({GLOBAL_SEGMENT: all_normal_values})
            global_row = global_rows[0]
            global_row.tier = EFFECTIVE_TIER
            global_row.granularity = "flat"
            global_row.recompute_checksum()
            new_model.rows = [
                row
                for row in new_model.rows
                if not (row.segment_id == GLOBAL_SEGMENT and row.metric == "score" and row.tier == EFFECTIVE_TIER)
            ]
            new_model.rows.append(global_row)

        return new_model

    def _propagation_plan(
        self,
        model: ThresholdModel,
        new_model: ThresholdModel,
        affected_segments: list[str],
        feature_index: dict[str, dict[str, Any]],
        request: CalibrationRequest,
        changed_ids: list[str],
    ) -> dict[str, Any]:
        affected_set = set(affected_segments)
        old_segment_checksums = {
            row.segment_id: row.checksum
            for row in model.rows
            if row.tier == EFFECTIVE_TIER and row.metric == "score"
        }
        new_segment_checksums = {
            row.segment_id: row.checksum
            for row in new_model.rows
            if row.tier == EFFECTIVE_TIER and row.metric == "score"
        }
        recompute_segments = sorted(
            segment
            for segment, checksum in new_segment_checksums.items()
            if segment == GLOBAL_SEGMENT
            or segment in affected_set
            or old_segment_checksums.get(segment) != checksum
        )
        reuse_segments = sorted(
            segment
            for segment in old_segment_checksums
            if segment not in recompute_segments and segment != GLOBAL_SEGMENT
        )
        recompute_window_ids = sorted(
            window_id
            for window_id, item in feature_index.items()
            if item.get("segment_id") in recompute_segments
        )
        reuse_window_ids = sorted(
            window_id
            for window_id, item in feature_index.items()
            if item.get("segment_id") in reuse_segments
        )
        drift = request.drift_alert or {}
        impacted_modules = list(drift.get("module_set", [])) or [
            "pseudo_label_store",
            "threshold_model",
            "adjudication",
        ]
        return {
            "reason": request.reason,
            "changed_window_ids": changed_ids,
            "affected_time_windows": drift.get("affected_time_windows", []),
            "time_window": drift.get("time_window"),
            "impacted_module_set": sorted(set(impacted_modules)),
            "recompute": {
                "segments": recompute_segments,
                "threshold_rows": [
                    f"{segment}:effective:score" for segment in recompute_segments
                ],
                "decisions": recompute_window_ids,
                "modules": ["calibration_scheduler", "threshold_model", "adjudication", "threshold_snapshot"],
            },
            "reuse": {
                "segments": reuse_segments,
                "decisions": reuse_window_ids,
                "feature_artifacts": [f"feature_cache:{window_id}" for window_id in reuse_window_ids],
                "modules": [
                    "window_preprocessing",
                    "mask_completion",
                    "autoencoder_weights",
                    "feature_extraction",
                    "drift_monitor",
                    "plan_snapshot",
                    "result_snapshot",
                ],
            },
            "checksum_changes": {
                segment: {
                    "old": old_segment_checksums.get(segment),
                    "new": new_segment_checksums.get(segment),
                }
                for segment in recompute_segments
                if segment in old_segment_checksums or segment in new_segment_checksums
            },
        }
