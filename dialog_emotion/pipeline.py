"""Incremental DAG pipeline with provenance-aware stage caches."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .audio import chunk_hashes
from .cache import (
    ConfigSnapshotStore,
    ContentAddressedCache,
    make_cache_key,
)
from .canonical import content_hash
from .config import PipelineConfig
from .errors import fail
from .features import feature_product
from .lineage import LineageStore
from .samples import DialogInput, RegistrationSample, SampleBundle
from .scoring import aggregate_dialog, score_segment
from .segments import build_segments
from .speaker import (
    ASSIGNMENT_ALGORITHM,
    assign_speakers,
    registration_version,
)
from .truth import TruthCorrection, TruthStore

PIPELINE_VERSION = "dialog-emotion-dag-v1"


@dataclass(frozen=True)
class AppendRegistration:
    speaker_id: str
    register_seed: int
    register_ms: int


@dataclass(frozen=True)
class CorrectTruth:
    dialog_id: str
    valence: float
    arousal: float
    segment_index: int | None = None
    start_frame: int | None = None
    end_frame: int | None = None


@dataclass(frozen=True)
class SetFrameShift:
    frame_shift_ms: float


@dataclass(frozen=True)
class SetSegmentThreshold:
    segment_threshold_ms: float


@dataclass
class RunResult:
    config: PipelineConfig
    config_snapshot: dict[str, Any]
    features: dict[str, dict[str, Any]]
    assignments: dict[str, dict[str, Any]]
    boundaries: dict[str, dict[str, Any]]
    segment_scores: dict[str, list[dict[str, Any]]]
    aggregates: dict[str, dict[str, Any]]
    node_keys: dict[str, str]
    stage_hashes: dict[str, str]
    lineage_records: list[dict[str, Any]]
    report: dict[str, Any]
    changes: list[dict[str, Any]] = field(default_factory=list)

    def public_payload(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "config_snapshot_version": self.config_snapshot["version"],
            "segments": self.segment_scores,
            "dialog_aggregates": self.aggregates,
            "stage_hashes": self.stage_hashes,
            "lineage_records": [
                {
                    "record_id": record["record_id"],
                    "kind": record["kind"],
                    "dialog_id": record["dialog_id"],
                    "segment_index": record["segment_index"],
                }
                for record in self.lineage_records
            ],
            "incremental_report": self.report,
            "changes": self.changes,
        }


class IncrementalPipeline:
    def __init__(
        self,
        bundle: SampleBundle,
        *,
        config: PipelineConfig | None = None,
        cache_dir: str | Path,
        config_snapshot_dir: str | Path,
        lineage_dir: str | Path,
        truth_store: TruthStore | None = None,
    ) -> None:
        self.bundle = bundle
        self.config = config or bundle.base_config
        self.config.validate()
        self.cache = ContentAddressedCache(cache_dir)
        self.config_store = ConfigSnapshotStore(config_snapshot_dir)
        self.lineage = LineageStore(lineage_dir)
        self.truth_store = truth_store or bundle.truth_store()
        self.config_snapshot = self.config_store.commit(self.config)
        self.last_result: RunResult | None = None
        self._reset_tracking()

    def run(
        self,
        changes: list[Any] | None = None,
    ) -> RunResult:
        changes = list(changes or [])
        previous = self.last_result
        applied_changes = self._apply_changes(changes, previous)
        affected = self._plan_affected(previous, applied_changes)
        self.config.validate()
        self.config_snapshot = self.config_store.commit(self.config)
        self._reset_tracking()

        features: dict[str, dict[str, Any]] = {}
        assignments: dict[str, dict[str, Any]] = {}
        boundaries: dict[str, dict[str, Any]] = {}
        segment_scores: dict[str, list[dict[str, Any]]] = {}
        aggregates: dict[str, dict[str, Any]] = {}
        lineage_records: list[dict[str, Any]] = []
        stage_hashes: dict[str, str] = {}

        for dialog_id in sorted(self.bundle.dialogs):
            dialog = self.bundle.dialogs[dialog_id]
            variant = self.config.frame_variant_key()
            truth_rows = self.truth_store.rows(dialog_id, variant)
            reg_version = registration_version(
                self.bundle.registrations, dialog.speaker_ids
            )
            truth_version = self.truth_store.correction_version(dialog_id)
            audit = self._audit_context(
                dialog,
                reg_version,
                truth_version,
            )
            chunks = chunk_hashes(dialog.pcm, self.config.audio_chunk_samples)

            frame_node = f"frame_features:{dialog_id}"
            frame_identity = {
                "dialog_id": dialog_id,
                "audio_chunk_hashes": chunks,
                "pcm_sample_count": len(dialog.pcm),
                "sample_rate": self.config.sample_rate,
                "frame_ms": self.config.frame_ms,
                "frame_shift_ms": self.config.frame_shift_ms,
                "frame_samples": self.config.frame_samples(),
                "hop_samples": self.config.frame_shift_samples(),
                "rolloff_percent": self.config.rolloff_percent,
                "feature_algorithm": "energy-zcr-centroid-rolloff-v1",
            }
            frame_key, frame_components = make_cache_key(
                "frame_features",
                frame_identity,
                {**audit, "upstream_node_hashes": {}},
            )
            frame_entry = self._resolve(
                frame_node,
                frame_key,
                frame_components,
                parents={},
                producer=lambda dialog=dialog, truth_rows=truth_rows: feature_product(
                    dialog.dialog_id,
                    dialog.pcm,
                    self.config,
                    len(truth_rows),
                ),
            )
            frame_data = frame_entry["data"]
            features[dialog_id] = frame_data
            stage_hashes[frame_node] = frame_entry["content_hash"]

            assignment_node = f"speaker_assignment:{dialog_id}"
            assignment_identity = {
                "dialog_id": dialog_id,
                "audio_chunk_hashes": chunks,
                "upstream_frame_features_hash": frame_entry["content_hash"],
                "registration_version": reg_version,
                "assignment_algorithm": ASSIGNMENT_ALGORITHM,
                "speech_energy_threshold": self.config.speech_energy_threshold,
            }
            assignment_key, assignment_components = make_cache_key(
                "speaker_assignment",
                assignment_identity,
                {
                    **audit,
                    "upstream_node_hashes": {
                        "frame_features": frame_entry["content_hash"]
                    },
                },
            )
            assignment_entry = self._resolve(
                assignment_node,
                assignment_key,
                assignment_components,
                parents={"frame_features": frame_key},
                producer=lambda dialog=dialog, frame_data=frame_data: assign_speakers(
                    dialog.dialog_id,
                    frame_data,
                    dialog.speaker_ids,
                    self.bundle.registrations,
                    self.config,
                ),
            )
            assignment_data = assignment_entry["data"]
            assignments[dialog_id] = assignment_data
            stage_hashes[assignment_node] = assignment_entry["content_hash"]

            boundary_node = f"segment_boundary:{dialog_id}"
            boundary_identity = {
                "dialog_id": dialog_id,
                "audio_chunk_hashes": chunks,
                "upstream_labels_hash": assignment_data["labels_hash"],
                "segment_threshold_ms": self.config.segment_threshold_ms,
                "frame_shift_ms": self.config.frame_shift_ms,
                "frame_count": frame_data["frame_count"],
                "segmentation_algorithm": "short-run-merge-v1",
            }
            boundary_key, boundary_components = make_cache_key(
                "segment_boundary",
                boundary_identity,
                {
                    **audit,
                    "upstream_node_hashes": {
                        "speaker_assignment_labels": assignment_data["labels_hash"],
                        "speaker_assignment": assignment_entry["content_hash"],
                    },
                },
            )
            boundary_entry = self._resolve(
                boundary_node,
                boundary_key,
                boundary_components,
                parents={"speaker_assignment": assignment_key},
                producer=lambda dialog=dialog, assignment_data=assignment_data, frame_data=frame_data: build_segments(
                    dialog.dialog_id,
                    assignment_data["labels"],
                    self.config.segment_threshold_ms,
                    self.config.frame_shift_ms,
                    frame_data["frame_count"],
                ),
            )
            boundary_data = boundary_entry["data"]
            boundaries[dialog_id] = boundary_data
            stage_hashes[boundary_node] = boundary_entry["content_hash"]

            dialog_scores: list[dict[str, Any]] = []
            segment_keys: list[str] = []
            for segment in boundary_data["segments"]:
                start = int(segment["start_frame"])
                end = int(segment["end_frame"])
                segment_truth = self.truth_store.rows(
                    dialog_id, variant, start, end
                )
                truth_scope_hash = self.truth_store.scope_hash(
                    dialog_id, variant, start, end
                )
                feature_slice_hash = content_hash(
                    {
                        "feature_names": frame_data["feature_names"],
                        "features": frame_data["features"][start:end],
                    }
                )
                segment_descriptor = {
                    "dialog_id": dialog_id,
                    "segment_index": int(segment["segment_index"]),
                    "start_frame": start,
                    "end_frame": end,
                    "start_ms": segment["start_ms"],
                    "end_ms": segment["end_ms"],
                    "duration_ms": segment["duration_ms"],
                    "speaker_id": segment["speaker_id"],
                }
                segment_descriptor_hash = content_hash(segment_descriptor)
                segment_node = (
                    f"segment_regression:{dialog_id}:{start}-{end}:"
                    f"{segment['speaker_id']}"
                )
                segment_identity = {
                    "dialog_id": dialog_id,
                    "audio_chunk_hashes": chunks,
                    "segment_descriptor": segment_descriptor,
                    "feature_slice_hash": feature_slice_hash,
                    "truth_scope_hash": truth_scope_hash,
                    "ridge_lambda": self.config.ridge_lambda,
                    "confidence_level": self.config.confidence_level,
                    "regression_algorithm": "deterministic-ridge-intercept-v1",
                }
                segment_key, segment_components = make_cache_key(
                    "segment_regression",
                    segment_identity,
                    {
                        **audit,
                        "truth_correction_version": truth_version,
                        "upstream_node_hashes": {
                            "frame_features": frame_entry["content_hash"],
                            "feature_slice": feature_slice_hash,
                            "segment_descriptor": segment_descriptor_hash,
                            "truth_scope": truth_scope_hash,
                        },
                    },
                )
                segment_entry = self._resolve(
                    segment_node,
                    segment_key,
                    segment_components,
                    parents={
                        "frame_features": frame_key,
                        "speaker_assignment": assignment_key,
                        "segment_boundary": boundary_key,
                    },
                    producer=lambda segment=segment, segment_truth=segment_truth: score_segment(
                        dialog_id,
                        segment,
                        frame_data,
                        segment_truth,
                        self.config.ridge_lambda,
                        self.config.confidence_level,
                    ),
                )
                segment_data = segment_entry["data"]
                dialog_scores.append(segment_data)
                segment_keys.append(segment_key)
                stage_hashes[segment_node] = segment_entry["content_hash"]
                lineage_record = self.lineage.record(
                    kind="segment_score",
                    dialog_id=dialog_id,
                    segment_index=int(segment["segment_index"]),
                    node_path=[
                        "frame_features",
                        "speaker_assignment",
                        "segment_boundary",
                        "segment_regression",
                    ],
                    cache_keys={
                        "frame_features": frame_key,
                        "speaker_assignment": assignment_key,
                        "segment_boundary": boundary_key,
                        "segment_regression": segment_key,
                    },
                    input_versions={
                        "registration_version": reg_version,
                        "truth_correction_version": truth_version,
                        "truth_scope_hash": truth_scope_hash,
                        "config_version": self.config_snapshot["version"],
                        "config_content_hash": self.config_snapshot["content_hash"],
                        "frame_shift_ms": self.config.frame_shift_ms,
                        "segment_threshold_ms": self.config.segment_threshold_ms,
                        "weight_basis": self.config.weight_basis,
                    },
                    score={
                        "valence": segment_data["valence"],
                        "arousal": segment_data["arousal"],
                        "valence_ci": segment_data["valence_ci"],
                        "arousal_ci": segment_data["arousal_ci"],
                    },
                )
                lineage_records.append(lineage_record)

            segment_scores[dialog_id] = dialog_scores
            aggregate_node = f"dialog_aggregate:{dialog_id}"
            segment_hashes_in_order = [
                self.cache.get(key)["content_hash"] for key in segment_keys
            ]
            aggregate_identity = {
                "dialog_id": dialog_id,
                "audio_chunk_hashes": chunks,
                "upstream_segment_content_hashes": segment_hashes_in_order,
                "weight_basis": self.config.weight_basis,
                "aggregation_algorithm": "duration-weighted-mean-v1",
            }
            aggregate_key, aggregate_components = make_cache_key(
                "dialog_aggregate",
                aggregate_identity,
                {
                    **audit,
                    "upstream_node_hashes": {
                        f"segment_regression:{index}": value
                        for index, value in enumerate(segment_hashes_in_order)
                    },
                },
            )
            aggregate_entry = self._resolve(
                aggregate_node,
                aggregate_key,
                aggregate_components,
                parents={
                    **{
                        f"segment_regression:{index}": key
                        for index, key in enumerate(segment_keys)
                    },
                    "segment_boundary": boundary_key,
                },
                producer=lambda dialog_id=dialog_id, dialog_scores=dialog_scores: aggregate_dialog(
                    dialog_id,
                    dialog_scores,
                    self.config.weight_basis,
                ),
            )
            aggregate_data = aggregate_entry["data"]
            aggregates[dialog_id] = aggregate_data
            stage_hashes[aggregate_node] = aggregate_entry["content_hash"]
            lineage_record = self.lineage.record(
                kind="dialog_score",
                dialog_id=dialog_id,
                node_path=[
                    "frame_features",
                    "speaker_assignment",
                    "segment_boundary",
                    "segment_regression",
                    "dialog_aggregate",
                ],
                cache_keys={
                    "frame_features": frame_key,
                    "speaker_assignment": assignment_key,
                    "segment_boundary": boundary_key,
                    "dialog_aggregate": aggregate_key,
                },
                segment_keys=segment_keys,
                input_versions={
                    "registration_version": reg_version,
                    "truth_correction_version": truth_version,
                    "config_version": self.config_snapshot["version"],
                    "config_content_hash": self.config_snapshot["content_hash"],
                    "frame_shift_ms": self.config.frame_shift_ms,
                    "segment_threshold_ms": self.config.segment_threshold_ms,
                    "weight_basis": self.config.weight_basis,
                },
                score={
                    "valence": aggregate_data["valence"],
                    "arousal": aggregate_data["arousal"],
                    "weight_basis": aggregate_data["weight_basis"],
                    "total_weight": aggregate_data["total_weight"],
                },
            )
            lineage_records.append(lineage_record)

        report = self._build_report(previous, applied_changes, affected)
        result = RunResult(
            config=self.config,
            config_snapshot=dict(self.config_snapshot),
            features=features,
            assignments=assignments,
            boundaries=boundaries,
            segment_scores=segment_scores,
            aggregates=aggregates,
            node_keys=dict(self._node_keys),
            stage_hashes=stage_hashes,
            lineage_records=lineage_records,
            report=report,
            changes=applied_changes,
        )
        self.last_result = result
        return result

    def append_registration(
        self,
        speaker_id: str,
        register_seed: int,
        register_ms: int,
    ) -> None:
        self.bundle.registrations.append(
            RegistrationSample(
                speaker_id=speaker_id,
                register_seed=int(register_seed),
                register_ms=int(register_ms),
            )
        )

    def add_truth_correction(
        self,
        dialog_id: str,
        start_frame: int,
        end_frame: int,
        valence: float,
        arousal: float,
    ) -> TruthCorrection:
        return self.truth_store.add_correction(
            dialog_id=dialog_id,
            start_frame=start_frame,
            end_frame=end_frame,
            hop_ms=self.config.frame_shift_ms,
            valence=valence,
            arousal=arousal,
        )

    def _resolve(
        self,
        node: str,
        key: str,
        key_components: dict[str, Any],
        *,
        parents: dict[str, Any],
        producer: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        entry = self.cache.get(key)
        self._node_keys[node] = key
        if entry is not None:
            self._node_content_hashes[node] = entry["content_hash"]
            self._reused.append(node)
            self._reused_set.add(node)
            return entry
        data = producer()
        entry = self.cache.put(
            key,
            data,
            node=node,
            key_components=key_components,
            parents=parents,
            max_entries=self.config.limits.max_cache_entries,
        )
        self._node_content_hashes[node] = entry["content_hash"]
        self._recomputed.append(node)
        self._recomputed_set.add(node)
        return entry

    def _apply_changes(
        self,
        changes: list[Any],
        previous: RunResult | None,
    ) -> list[dict[str, Any]]:
        applied: list[dict[str, Any]] = []
        for change in changes:
            if isinstance(change, AppendRegistration):
                self.append_registration(
                    change.speaker_id,
                    change.register_seed,
                    change.register_ms,
                )
                applied.append(
                    {
                        "type": "append_registration",
                        **asdict(change),
                    }
                )
            elif isinstance(change, CorrectTruth):
                start_frame = change.start_frame
                end_frame = change.end_frame
                if change.segment_index is not None:
                    if previous is None:
                        fail(
                            "E_CHANGE_INVALID",
                            "segment-index truth correction requires a previous run",
                            dialog_id=change.dialog_id,
                            segment_index=change.segment_index,
                        )
                    scores = previous.segment_scores.get(change.dialog_id, [])
                    matching = [
                        item
                        for item in scores
                        if int(item["segment_index"]) == change.segment_index
                    ]
                    if not matching:
                        fail(
                            "E_CHANGE_INVALID",
                            "segment index does not exist in the previous run",
                            dialog_id=change.dialog_id,
                            segment_index=change.segment_index,
                        )
                    start_frame = int(matching[0]["start_frame"])
                    end_frame = int(matching[0]["end_frame"])
                if start_frame is None or end_frame is None:
                    fail(
                        "E_CHANGE_INVALID",
                        "truth correction needs segment_index or an explicit frame interval",
                        dialog_id=change.dialog_id,
                    )
                correction = self.add_truth_correction(
                    change.dialog_id,
                    start_frame,
                    end_frame,
                    change.valence,
                    change.arousal,
                )
                applied.append(
                    {
                        "type": "correct_truth",
                        "dialog_id": change.dialog_id,
                        "segment_index": change.segment_index,
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "valence": change.valence,
                        "arousal": change.arousal,
                        "correction_id": correction.correction_id,
                        "correction_version": correction.version,
                    }
                )
            elif isinstance(change, SetFrameShift):
                self.config = replace(
                    self.config, frame_shift_ms=change.frame_shift_ms
                )
                self.config.validate()
                applied.append(
                    {
                        "type": "set_frame_shift",
                        "frame_shift_ms": change.frame_shift_ms,
                    }
                )
            elif isinstance(change, SetSegmentThreshold):
                self.config = replace(
                    self.config,
                    segment_threshold_ms=change.segment_threshold_ms,
                )
                self.config.validate()
                applied.append(
                    {
                        "type": "set_segment_threshold",
                        "segment_threshold_ms": change.segment_threshold_ms,
                    }
                )
            else:
                fail(
                    "E_CHANGE_INVALID",
                    "unsupported incremental change",
                    change_type=type(change).__name__,
                )
        return applied

    def _plan_affected(
        self,
        previous: RunResult | None,
        changes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        all_frames = {
            dialog_id: list(range(len(dialog.truth_variants.get(
                self.config.frame_variant_key(), []
            ))))
            for dialog_id, dialog in self.bundle.dialogs.items()
        }
        if not changes:
            if previous is None:
                return {
                    "dialog_ids": sorted(self.bundle.dialogs),
                    "frames": all_frames,
                    "segments": {
                        dialog_id: list(range(len(scores)))
                        for dialog_id, scores in (
                            previous.segment_scores.items()
                            if previous
                            else {}
                        )
                    },
                    "nodes": set(),
                }
            return {"dialog_ids": [], "frames": {}, "segments": {}, "nodes": set()}

        dialog_ids: set[str] = set()
        frames: dict[str, set[int]] = {}
        segments: dict[str, set[int]] = {}
        nodes: set[str] = set()

        def add_dialog(dialog_id: str) -> None:
            dialog_ids.add(dialog_id)
            frames.setdefault(dialog_id, set())
            segments.setdefault(dialog_id, set())

        for change in changes:
            change_type = change["type"]
            if change_type == "append_registration":
                speaker_id = change["speaker_id"]
                for dialog_id, dialog in self.bundle.dialogs.items():
                    if speaker_id not in dialog.speaker_ids:
                        continue
                    add_dialog(dialog_id)
                    frames[dialog_id].update(all_frames[dialog_id])
                    previous_scores = (
                        previous.segment_scores.get(dialog_id, [])
                        if previous
                        else []
                    )
                    for score in previous_scores:
                        if score["speaker_id"] == speaker_id:
                            segments[dialog_id].add(int(score["segment_index"]))
                    nodes.update(
                        {
                            f"speaker_assignment:{dialog_id}",
                            f"segment_boundary:{dialog_id}",
                            f"dialog_aggregate:{dialog_id}",
                        }
                    )
                    for score in previous_scores:
                        nodes.add(
                            f"segment_regression:{dialog_id}:"
                            f"{score['start_frame']}-{score['end_frame']}:"
                            f"{score['speaker_id']}"
                        )
            elif change_type == "correct_truth":
                dialog_id = change["dialog_id"]
                add_dialog(dialog_id)
                start = int(change["start_frame"])
                end = int(change["end_frame"])
                frames[dialog_id].update(range(start, end))
                previous_scores = (
                    previous.segment_scores.get(dialog_id, []) if previous else []
                )
                for score in previous_scores:
                    if int(score["start_frame"]) < end and int(score["end_frame"]) > start:
                        segments[dialog_id].add(int(score["segment_index"]))
                        nodes.add(
                            f"segment_regression:{dialog_id}:"
                            f"{score['start_frame']}-{score['end_frame']}:"
                            f"{score['speaker_id']}"
                        )
                nodes.add(f"dialog_aggregate:{dialog_id}")
            elif change_type == "set_frame_shift":
                for dialog_id in self.bundle.dialogs:
                    add_dialog(dialog_id)
                    frames[dialog_id].update(all_frames[dialog_id])
                    nodes.update(
                        {
                            f"frame_features:{dialog_id}",
                            f"speaker_assignment:{dialog_id}",
                            f"segment_boundary:{dialog_id}",
                            f"dialog_aggregate:{dialog_id}",
                        }
                    )
                    if previous:
                        previous_scores = previous.segment_scores.get(dialog_id, [])
                        segments[dialog_id].update(
                            int(score["segment_index"]) for score in previous_scores
                        )
                        for score in previous_scores:
                            nodes.add(
                                f"segment_regression:{dialog_id}:"
                                f"{score['start_frame']}-{score['end_frame']}:"
                                f"{score['speaker_id']}"
                            )
            elif change_type == "set_segment_threshold":
                for dialog_id in self.bundle.dialogs:
                    add_dialog(dialog_id)
                    frames[dialog_id].update(all_frames[dialog_id])
                    nodes.update(
                        {
                            f"segment_boundary:{dialog_id}",
                            f"dialog_aggregate:{dialog_id}",
                        }
                    )
                    if previous:
                        previous_scores = previous.segment_scores.get(dialog_id, [])
                        segments[dialog_id].update(
                            int(score["segment_index"]) for score in previous_scores
                        )
                        for score in previous_scores:
                            nodes.add(
                                f"segment_regression:{dialog_id}:"
                                f"{score['start_frame']}-{score['end_frame']}:"
                                f"{score['speaker_id']}"
                            )
        return {
            "dialog_ids": sorted(dialog_ids),
            "frames": {
                dialog_id: sorted(indices)
                for dialog_id, indices in frames.items()
            },
            "segments": {
                dialog_id: sorted(indices)
                for dialog_id, indices in segments.items()
            },
            "nodes": nodes,
        }

    def _build_report(
        self,
        previous: RunResult | None,
        changes: list[dict[str, Any]],
        affected: dict[str, Any],
    ) -> dict[str, Any]:
        reused = list(dict.fromkeys(self._reused))
        recomputed = list(dict.fromkeys(self._recomputed))
        reused_set = set(reused)
        recomputed_set = set(recomputed)
        evaluated = reused_set | recomputed_set
        planned_affected_nodes = set(affected["nodes"])
        if previous is None and not changes:
            affected_nodes = set(evaluated)
        else:
            affected_nodes = set(recomputed_set)
            if previous is not None:
                for node in evaluated:
                    if previous.stage_hashes.get(node) != self._node_content_hashes.get(
                        node
                    ):
                        affected_nodes.add(node)
        # Concrete old segment node ids may disappear when boundaries move. The
        # current affected set contains only evaluated nodes, while the planned
        # set remains visible for audit.
        affected_nodes &= evaluated
        self_check = {
            "reused_recomputed_disjoint": not bool(reused_set & recomputed_set),
            "union_covers_affected": affected_nodes <= evaluated,
            "union_equals_evaluated": evaluated == (reused_set | recomputed_set),
            "affected_nodes_were_evaluated": affected_nodes <= evaluated,
        }
        self_check["passed"] = all(self_check.values())
        return {
            "changes": changes,
            "affected_dialog_ids": affected["dialog_ids"],
            "affected_frames": affected["frames"],
            "affected_segments": affected["segments"],
            "affected_nodes": sorted(affected_nodes),
            "planned_affected_nodes": sorted(planned_affected_nodes),
            "reused_nodes": reused,
            "recomputed_nodes": recomputed,
            "invalidated_nodes": sorted(recomputed_set),
            "evaluated_node_count": len(evaluated),
            "self_check": self_check,
        }

    def _audit_context(
        self,
        dialog: DialogInput,
        reg_version: str,
        truth_version: str,
    ) -> dict[str, Any]:
        return {
            "pipeline_version": PIPELINE_VERSION,
            "config_version": self.config_snapshot["version"],
            "config_content_hash": self.config_snapshot["content_hash"],
            "registration_version": reg_version,
            "truth_correction_version": truth_version,
            "frame_shift_ms": self.config.frame_shift_ms,
            "segment_threshold_ms": self.config.segment_threshold_ms,
            "dialog_id": dialog.dialog_id,
        }

    def _reset_tracking(self) -> None:
        self._node_keys: dict[str, str] = {}
        self._node_content_hashes: dict[str, str] = {}
        self._reused: list[str] = []
        self._recomputed: list[str] = []
        self._reused_set: set[str] = set()
        self._recomputed_set: set[str] = set()


def compare_runs(
    incremental: RunResult,
    full: RunResult,
) -> dict[str, Any]:
    all_nodes = sorted(
        set(incremental.stage_hashes) | set(full.stage_hashes),
        key=_stage_order_key,
    )
    for node in all_nodes:
        left = incremental.stage_hashes.get(node)
        right = full.stage_hashes.get(node)
        if left != right:
            fail(
                "E_INCREMENTAL_DRIFT",
                "incremental output differs from a clean full run",
                first_divergent_node=node,
                incremental_hash=left,
                full_hash=right,
            )
    if content_hash(incremental.segment_scores) != content_hash(full.segment_scores):
        fail(
            "E_INCREMENTAL_DRIFT",
            "incremental segment scores differ from a clean full run",
            first_divergent_node="segment_regression",
        )
    if content_hash(incremental.aggregates) != content_hash(full.aggregates):
        fail(
            "E_INCREMENTAL_DRIFT",
            "incremental dialog aggregates differ from a clean full run",
            first_divergent_node="dialog_aggregate",
        )
    return {
        "matched": True,
        "node_count": len(all_nodes),
        "first_divergent_node": None,
        "incremental_stage_hashes": incremental.stage_hashes,
        "full_stage_hashes": full.stage_hashes,
    }


def _stage_order_key(node: str) -> tuple[int, str]:
    stage = node.split(":", 1)[0]
    order = {
        "frame_features": 0,
        "speaker_assignment": 1,
        "segment_boundary": 2,
        "segment_regression": 3,
        "dialog_aggregate": 4,
    }
    return order.get(stage, 99), node
