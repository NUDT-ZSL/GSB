"""Content-addressed feature cache and selective derived-artifact rebuilds."""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

import numpy as np

from .aggregation import build_view, finalize_view
from .config import EngineConfig
from .features import (
    extract_features,
    feature_cache_key,
    pcm_content_hash,
    validate_speakers,
)
from .segments import build_segments
from .tiering import classify_sequence
from .util import (
    complement_intervals,
    intervals_cover_all,
    intervals_overlap,
    merge_intervals,
    sha256_json,
)
from .windows import compute_window, window_bounds


VIEW_BUILDER_VERSION = "view-builder-v1"


class ArtifactCache:
    """Two-layer cache: PCM/features, then derived frame/window/segment views."""

    def __init__(self) -> None:
        self._features: dict[str, list[dict[str, Any]]] = {}
        self._view_by_key: dict[str, dict[str, Any]] = {}
        self._base_view: dict[str, tuple[EngineConfig, dict[str, Any]]] = {}
        self.feature_hits = 0
        self.feature_misses = 0
        self.view_hits = 0
        self.view_misses = 0
        self.selective_rebuilds = 0
        self.full_rebuilds = 0

    def features_for_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        config: EngineConfig,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pcm_hash = pcm_content_hash(pcm, sample_rate)
        key = feature_cache_key(pcm_hash, sample_rate, config)
        if key in self._features:
            self.feature_hits += 1
            return copy.deepcopy(self._features[key]), {
                "feature_cache_key": key,
                "pcm_content_key": pcm_hash,
                "feature_cache_hit": True,
                "feature_recomputed": False,
            }
        self.feature_misses += 1
        features = extract_features(pcm, sample_rate, config)
        self._features[key] = copy.deepcopy(features)
        return features, {
            "feature_cache_key": key,
            "pcm_content_key": pcm_hash,
            "feature_cache_hit": False,
            "feature_recomputed": True,
        }

    def derived_key(
        self,
        feature_key: str,
        speakers: Sequence[str],
        config: EngineConfig,
    ) -> str:
        return sha256_json(
            {
                "feature_cache_key": feature_key,
                "speaker_sequence_sha256": sha256_json([str(item) for item in speakers]),
                "view_builder_version": VIEW_BUILDER_VERSION,
                "window_signature": config.window_signature(),
                "threshold_policy": config.threshold_policy_signature(),
                "tier_schema_version": str(config.tier_schema["version"]),
                "tier_schema_sha256": config.tier_schema_hash(),
            }
        )

    def build_view(
        self,
        features: Sequence[Mapping[str, Any]],
        speakers: Sequence[str],
        config: EngineConfig,
        feature_key: str,
        identity: Mapping[str, Any] | None = None,
        allowed_speakers: Sequence[str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        speakers = [str(item) for item in speakers]
        declared = (
            [str(item) for item in allowed_speakers]
            if allowed_speakers is not None
            else speakers
        )
        validate_speakers(speakers, declared, len(features))
        key = self.derived_key(feature_key, speakers, config)
        base_key = sha256_json(
            {
                "feature_cache_key": feature_key,
                "speaker_sequence_sha256": sha256_json(speakers),
            }
        )
        if key in self._view_by_key:
            self.view_hits += 1
            view = copy.deepcopy(self._view_by_key[key])
            view["identity"] = dict(identity or {})
            return view, self._report(
                key,
                feature_key,
                all_reused=True,
                selective=False,
                total=len(features),
                recomputed=[],
                segment_ids=[],
            )

        self.view_misses += 1
        previous = self._base_view.get(base_key)
        if previous is None:
            self.full_rebuilds += 1
            view = build_view(
                features,
                speakers,
                config,
                identity=identity,
                allowed_speakers=allowed_speakers,
            )
            recomputed = [[0, len(features) - 1]] if features else []
            report = self._report(
                key,
                feature_key,
                all_reused=False,
                selective=False,
                total=len(features),
                recomputed=recomputed,
                segment_ids=[str(segment["segment_id"]) for segment in view["segments"]],
            )
        else:
            self.selective_rebuilds += 1
            view, recomputed, segment_ids = self._selective_rebuild(
                previous_config=previous[0],
                previous_view=previous[1],
                features=features,
                speakers=speakers,
                config=config,
                identity=identity,
                allowed_speakers=allowed_speakers,
            )
            report = self._report(
                key,
                feature_key,
                all_reused=False,
                selective=True,
                total=len(features),
                recomputed=recomputed,
                segment_ids=segment_ids,
            )

        self._view_by_key[key] = copy.deepcopy(view)
        self._base_view[base_key] = (config, copy.deepcopy(view))
        return view, report

    def _selective_rebuild(
        self,
        previous_config: EngineConfig,
        previous_view: Mapping[str, Any],
        features: Sequence[Mapping[str, Any]],
        speakers: Sequence[str],
        config: EngineConfig,
        identity: Mapping[str, Any] | None,
        allowed_speakers: Sequence[str] | None,
    ) -> tuple[dict[str, Any], list[list[int]], list[str]]:
        old_frames = list(previous_view["frames"])
        count = len(features)
        if len(old_frames) != count:
            view = build_view(
                features,
                speakers,
                config,
                identity=identity,
                allowed_speakers=allowed_speakers,
            )
            return view, [[0, count - 1]] if count else [], [
                str(segment["segment_id"]) for segment in view["segments"]
            ]

        classified = classify_sequence(features, config)
        instant_changed = {
            index
            for index, (old, new) in enumerate(zip(old_frames, classified["tiers"]))
            if str(old["instant_tier"]) != str(new)
        }
        affected = self._affected_windows(
            old_frames,
            previous_config,
            config,
            instant_changed,
        )
        new_rows: list[dict[str, Any]] = []
        for index, old_frame in enumerate(old_frames):
            row = copy.deepcopy(old_frame)
            row["instant_tier"] = str(classified["tiers"][index])
            if index in affected:
                window = compute_window(index, [
                    {**old, "instant_tier": str(classified["tiers"][position])}
                    for position, old in enumerate(old_frames)
                ], config)
                row["window"] = window
            row["tier"] = str(row["window"]["tier"])
            new_rows.append(row)

        if affected:
            rebuilt_segments = build_segments(new_rows, config)
            old_segments = {
                str(segment["segment_id"]): segment
                for segment in previous_view["segments"]
            }
            segments = []
            segment_ids = []
            for rebuilt in rebuilt_segments:
                segment_id = str(rebuilt["segment_id"])
                intersects = any(
                    int(rebuilt["start_frame"]) <= frame_index <= int(rebuilt["end_frame"])
                    for frame_index in affected
                )
                old_segment = old_segments.get(segment_id)
                if not intersects and old_segment is not None:
                    segments.append(copy.deepcopy(old_segment))
                else:
                    segments.append(rebuilt)
                    segment_ids.append(segment_id)
        else:
            segments = copy.deepcopy(list(previous_view["segments"]))
            segment_ids = []
        view = finalize_view(new_rows, segments, classified, config, identity)
        recomputed = merge_intervals([[index, index] for index in sorted(affected)])
        return view, recomputed, segment_ids

    def _affected_windows(
        self,
        old_frames: Sequence[Mapping[str, Any]],
        old_config: EngineConfig,
        new_config: EngineConfig,
        instant_changed: set[int],
    ) -> set[int]:
        count = len(old_frames)
        if count == 0:
            return set()
        affected: set[int] = set()
        old_window = old_config.window_signature()
        new_window = new_config.window_signature()
        if (
            old_window["tie_rule"] != new_window["tie_rule"]
            or old_window["version"] != new_window["version"]
            or old_window["boundary_policy"] != new_window["boundary_policy"]
            or old_window["insufficient_policy"] != new_window["insufficient_policy"]
            or old_config.tier_schema_hash() != new_config.tier_schema_hash()
        ):
            affected.update(range(count))
        elif old_config.window_length != new_config.window_length:
            for index in range(count):
                old_start, _ = window_bounds(index, count, old_config.window_length)
                new_start, _ = window_bounds(index, count, new_config.window_length)
                old_size = index - old_start + 1
                new_size = index - new_start + 1
                old_insufficient = old_size < old_config.window_length
                new_insufficient = new_size < new_config.window_length
                if (
                    old_start != new_start
                    or old_size != new_size
                    or old_insufficient != new_insufficient
                ):
                    affected.add(index)
        max_window = max(old_config.window_length, new_config.window_length)
        for index in instant_changed:
            affected.update(range(index, min(count, index + max_window)))
        return affected

    def _report(
        self,
        view_key: str,
        feature_key: str,
        all_reused: bool,
        selective: bool,
        total: int,
        recomputed: Sequence[Sequence[int]],
        segment_ids: Sequence[str],
    ) -> dict[str, Any]:
        recomputed_merged = merge_intervals(recomputed)
        reused = [] if all_reused else complement_intervals(recomputed_merged, total)
        if all_reused:
            reused = [[0, total - 1]] if total else []
        if not intervals_cover_all(recomputed_merged, reused, total):
            raise AssertionError("cache recomputed/reused intervals must cover all frames")
        if intervals_overlap(recomputed_merged, reused):
            raise AssertionError("cache recomputed/reused intervals must not overlap")
        return {
            "derived_view_key": view_key,
            "feature_cache_key": feature_key,
            "mode": "full_reuse" if all_reused else ("selective_rebuild" if selective else "full_rebuild"),
            "recomputed_frame_intervals": recomputed_merged,
            "reused_frame_intervals": reused,
            "recomputed_segment_ids": [str(item) for item in segment_ids],
            "coverage_all_frames": True,
            "overlap_free": True,
            "feature_hits": self.feature_hits,
            "feature_misses": self.feature_misses,
            "view_hits": self.view_hits,
            "view_misses": self.view_misses,
            "selective_rebuilds": self.selective_rebuilds,
            "full_rebuilds": self.full_rebuilds,
        }
