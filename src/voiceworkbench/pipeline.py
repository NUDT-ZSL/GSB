"""Deterministic four-stage pipeline with fingerprint-driven incremental reuse."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .cache import Ledger, StageCache, make_stage_key
from .clustering import (
    Assignment,
    Cluster,
    agglomerative,
    assign_segments,
    merge_clusters,
    relabel,
    split_cluster,
)
from .config import PipelineConfig
from .determinism import content_hash, short_hash
from .errors import FailureRecord
from .features import FileFeatures, feature_artifact
from .materialize import MaterialState
from .vad import SpeechSegment, segment_all, segments_artifact


@dataclass
class PipelineResult:
    config_fingerprint: str
    material_fingerprint: str
    stage_keys: dict[str, str]
    features: list[FileFeatures]
    segments: list[SpeechSegment]
    clusters: list[Cluster]
    assignments: list[Assignment]
    failures: list[FailureRecord]
    ledger: Ledger
    cluster_overrides: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "speaker_count": len(self.clusters),
            "clusters": [cluster.to_dict() for cluster in self.clusters],
            "segment_count": len(self.segments),
            "assignments": [assignment.to_dict() for assignment in self.assignments],
            "affected_segments": sorted(
                segment.segment_id for segment in self.segments if segment.overlap
            ),
            "overlap_segments": [
                segment.to_dict() for segment in self.segments if segment.overlap
            ],
        }


class Pipeline:
    def __init__(self, config: PipelineConfig, cache: StageCache,
                 fault_injection: dict[str, Any] | None = None,
                 budget_check: Callable[[], None] | None = None) -> None:
        self.config = config
        self.cache = cache
        self.fault_injection = fault_injection or {}
        self.budget_check = budget_check or (lambda: None)
        self.failures: list[FailureRecord] = []

    def run(self, material: MaterialState, current_config: PipelineConfig,
            threshold_override: float | None,
            cluster_overrides: list[dict[str, Any]],
            ledger: Ledger,
            invalidate_reason: str | None) -> PipelineResult:
        self.config = current_config
        self.failures = []
        effective_threshold = (
            threshold_override
            if threshold_override is not None
            else current_config.cluster_distance_threshold
        )

        # ---- fingerprints / cache keys ----------------------------------
        material_fp = material.fingerprint()
        feature_params = content_hash({
            "frame_ms": current_config.frame_ms,
            "hop_ms": current_config.hop_ms,
            "voiceprint_dims": current_config.voiceprint_dims,
            "quant_levels": current_config.quant_levels,
            "sample_rate": current_config.sample_rate,
        })
        feature_key = make_stage_key([material_fp], feature_params)

        vad_params = content_hash({
            "energy_db_floor": current_config.energy_db_floor,
            "flatness_high": current_config.flatness_high,
            "flatness_low": current_config.flatness_low,
            "overlap_similarity": current_config.overlap_similarity,
            "min_speech_ms": current_config.min_speech_ms,
            "min_silence_ms": current_config.min_silence_ms,
            "hop_ms": current_config.hop_ms,
        })
        vad_key = make_stage_key([feature_key], vad_params)

        # Base agglomerative clustering depends only on segments + threshold.
        # Structural overrides (merge/split) then evolve deterministically on
        # top of the base clusters, so they are tracked as a separate key.
        base_cluster_params = content_hash({"threshold": effective_threshold})
        base_cluster_key = make_stage_key([vad_key], base_cluster_params)
        override_params = content_hash({"overrides": cluster_overrides})
        clustering_key = make_stage_key([base_cluster_key], override_params)

        assignment_params = content_hash({
            "max_workers": current_config.max_workers,
            "overrides_fingerprint": short_hash(cluster_overrides),
        })
        assignment_key = make_stage_key([clustering_key], assignment_params)

        if invalidate_reason is not None:
            self._apply_invalidation(invalidate_reason, ledger,
                                     [feature_key, vad_key, clustering_key, assignment_key])

        # ---- stage 1: features ------------------------------------------
        feature_entry = self.cache.get("features", feature_key)
        if feature_entry is not None:
            ledger.record_reuse("features", feature_key)
            features = self._rehydrate_features(feature_entry.artifact)
        else:
            self.budget_check()
            features, feature_failures = self._compute_features(material)
            self.failures.extend(feature_failures)
            self.cache.put(
                "features", feature_key, [material_fp], feature_params,
                feature_artifact(features, feature_failures), ledger,
            )

        # ---- stage 2: VAD ------------------------------------------------
        vad_entry = self.cache.get("vad", vad_key)
        if vad_entry is not None:
            ledger.record_reuse("vad", vad_key)
            segments = self._rehydrate_segments(vad_entry.artifact, features)
        else:
            self.budget_check()
            segments = segment_all(features, current_config)
            self.cache.put(
                "vad", vad_key, [feature_key], vad_params,
                segments_artifact(segments), ledger,
            )

        # ---- stage 3a: base agglomerative clustering --------------------
        base_entry = self.cache.get("clustering", base_cluster_key)
        if base_entry is not None:
            ledger.record_reuse("clustering", base_cluster_key)
            base_clusters = self._rehydrate_clusters(base_entry.artifact)
        else:
            self.budget_check()
            base_clusters = agglomerative(segments, effective_threshold)
            self.cache.put(
                "clustering", base_cluster_key, [vad_key], base_cluster_params,
                {"clusters": [cluster.to_dict() for cluster in base_clusters]},
                ledger,
            )

        # ---- stage 3b: deterministic structural override evolution ------
        cluster_entry = self.cache.get("clustering", clustering_key)
        if cluster_entry is not None:
            ledger.record_reuse("clustering", clustering_key)
            clusters = self._rehydrate_clusters(cluster_entry.artifact)
        else:
            self.budget_check()
            clusters = self._apply_overrides(base_clusters, segments,
                                             cluster_overrides)
            self.cache.put(
                "clustering", clustering_key, [base_cluster_key], override_params,
                {"clusters": [cluster.to_dict() for cluster in clusters]},
                ledger,
            )

        # ---- stage 4: assignment -----------------------------------------
        assignment_entry = self.cache.get("assignment", assignment_key)
        if assignment_entry is not None:
            ledger.record_reuse("assignment", assignment_key)
            assignments = self._rehydrate_assignments(assignment_entry.artifact)
        else:
            self.budget_check()
            assignments, assign_failures = assign_segments(
                segments, clusters, current_config, self.fault_injection,
            )
            self.failures.extend(assign_failures)
            self.cache.put(
                "assignment", assignment_key, [clustering_key], assignment_params,
                {"assignments": [item.to_dict() for item in assignments]}, ledger,
            )

        self.failures.sort(key=lambda item: item.sort_key)
        return PipelineResult(
            config_fingerprint=current_config.fingerprint(),
            material_fingerprint=material_fp,
            stage_keys={
                "features": feature_key,
                "vad": vad_key,
                "clustering": clustering_key,
                "assignment": assignment_key,
            },
            features=features,
            segments=segments,
            clusters=clusters,
            assignments=assignments,
            failures=list(self.failures),
            ledger=ledger,
            cluster_overrides=list(cluster_overrides),
        )

    # ------------------------------------------------------------------ #
    # Invalidation: only affected stages and their downstream stages.
    # ------------------------------------------------------------------ #
    def _apply_invalidation(self, reason: str, ledger: Ledger,
                            new_keys: list[str]) -> None:
        affected = {
            "audio_changed": ("features", "vad", "clustering", "assignment"),
            "cut_interval": ("features", "vad", "clustering", "assignment"),
            "replace_interval_audio": ("features", "vad", "clustering", "assignment"),
            "threshold_changed": ("clustering", "assignment"),
            "cluster_override": ("clustering", "assignment"),
        }.get(reason)
        if affected is None:
            return
        for stage in affected:
            self.cache.invalidate_stage(stage, ledger, reason)

    # ------------------------------------------------------------------ #
    # Computation helpers
    # ------------------------------------------------------------------ #
    def _compute_features(self, material: MaterialState
                          ) -> tuple[list[FileFeatures], list[FailureRecord]]:
        from .features import extract_samples
        all_features: list[FileFeatures] = []
        failures: list[FailureRecord] = []
        for name in sorted(material.files):
            result, file_failures = extract_samples(
                name, material.files[name], self.config, self.fault_injection,
            )
            all_features.append(result)
            failures.extend(file_failures)
        all_features.sort(key=lambda item: item.file_name)
        failures.sort(key=lambda item: item.sort_key)
        return all_features, failures

    def _apply_overrides(self, base_clusters: list[Cluster],
                         segments: list[SpeechSegment],
                         overrides: list[dict[str, Any]]) -> list[Cluster]:
        """Replay merge/split overrides from the cached base clustering.

        Selector labels are resolved against the *current* cluster set at each
        step, exactly as the interactive engine does, so an incremental replay
        and a blank serial replay converge to identical final clusters.
        """
        clusters = list(base_clusters)
        for override in overrides:
            labels = [cluster.label for cluster in clusters]
            if override["op"] == "merge":
                label_a = _resolve(override["label_a"], labels)
                label_b = _resolve(override["label_b"], labels)
                clusters = merge_clusters(clusters, label_a, label_b, segments)
            elif override["op"] == "split":
                label = _resolve(override["label"], labels)
                clusters = split_cluster(clusters, label, segments)
        return relabel(clusters, segments)

    # ------------------------------------------------------------------ #
    # Rehydration from cached plain-data artifacts
    # ------------------------------------------------------------------ #
    def _rehydrate_features(self, artifact: dict[str, Any]) -> list[FileFeatures]:
        from .features import FrameRecord
        files: list[FileFeatures] = []
        for raw in artifact["files"]:
            frames = [FrameRecord(**frame) for frame in raw["frames"]]
            files.append(FileFeatures(
                file_name=raw["file_name"],
                sample_count=raw["sample_count"],
                frame_count=raw["frame_count"],
                frames=frames,
                file_fingerprint=raw["file_fingerprint"],
            ))
        return sorted(files, key=lambda item: item.file_name)

    def _rehydrate_segments(self, artifact: dict[str, Any],
                            features: list[FileFeatures]) -> list[SpeechSegment]:
        return [SpeechSegment(**raw) for raw in artifact["segments"]]

    def _rehydrate_clusters(self, artifact: dict[str, Any]) -> list[Cluster]:
        return [Cluster(**raw) for raw in artifact["clusters"]]

    def _rehydrate_assignments(self, artifact: dict[str, Any]) -> list[Assignment]:
        return [Assignment(**raw) for raw in artifact["assignments"]]


def _resolve(selector: dict[str, Any], labels: list[str]) -> str:
    from .commands import resolve_selector
    return resolve_selector(selector, labels)
