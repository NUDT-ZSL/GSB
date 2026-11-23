"""Unified full-scene and region-crop pose post-processing pipeline."""

from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .cache import PipelineCache, make_cache_key
from .config import PipelineConfig
from .errors import BusinessError
from .grouping import (
    filter_instances_for_region,
    group_instances,
    validate_metadata,
)
from .heatmap import detect_and_refine, filter_peaks_to_region
from .io_utils import canonical_json_bytes
from .models import Instance, Peak, RegionSpec, SceneSample
from .scoring import scene_metrics, score_instances
from .snapshots import SnapshotBundle


@dataclass
class BatchResult:
    scene_id: str
    instances: list[Instance]
    peaks: list[Peak]
    metrics: dict[str, Any]
    elapsed_ms: float
    snapshot_versions: dict[str, int]
    snapshot_checksums: dict[str, str]
    node_path: list[str]
    cache_key: str
    cache_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self, score_decimals: int = 9) -> dict[str, Any]:
        return {
            "business_code": "OK",
            "scene_id": self.scene_id,
            "mode": "batch",
            "elapsed_ms": round(self.elapsed_ms, 6),
            "snapshot_versions": dict(sorted(self.snapshot_versions.items())),
            "snapshot_checksums": dict(sorted(self.snapshot_checksums.items())),
            "node_path": list(self.node_path),
            "cache_key": self.cache_key,
            "cache_stats": self.cache_stats,
            "metrics": self.metrics,
            "instances": [
                instance.record(score_decimals)
                for instance in sorted(
                    self.instances, key=lambda item: item.instance_id
                )
            ],
        }


@dataclass
class RegionResult:
    scene_id: str
    region: RegionSpec
    peaks: list[Peak]
    instances: list[Instance]
    metrics: dict[str, Any]
    elapsed_ms: float
    estimate: dict[str, Any]
    snapshot_versions: dict[str, int]
    snapshot_checksums: dict[str, str]
    node_path: list[str]
    cache_key: str
    cache_stats: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self, score_decimals: int = 9) -> dict[str, Any]:
        return {
            "business_code": "OK",
            "scene_id": self.scene_id,
            "mode": "region_preview",
            "region": self.region.to_dict(),
            "elapsed_ms": round(self.elapsed_ms, 6),
            "latency_estimate": self.estimate,
            "snapshot_versions": dict(sorted(self.snapshot_versions.items())),
            "snapshot_checksums": dict(sorted(self.snapshot_checksums.items())),
            "node_path": list(self.node_path),
            "cache_key": self.cache_key,
            "cache_stats": self.cache_stats,
            "metrics": self.metrics,
            "peaks": [peak.canonical() for peak in self.peaks],
            "instances": [
                instance.record(score_decimals)
                for instance in sorted(
                    self.instances, key=lambda item: item.instance_id
                )
            ],
        }


class PipelineEngine:
    def __init__(
        self,
        sample: SceneSample,
        bundle: SnapshotBundle,
        config: PipelineConfig,
        cache: PipelineCache,
    ):
        self.sample = sample
        self.bundle = bundle
        self.config = config
        self.cache = cache
        self._heatmaps: np.ndarray | None = None
        self._heatmaps_checksum = ""
        validate_metadata(
            bundle.metadata,
            expected_channels=sample.record.num_keypoints,
            expected_persons=sample.record.person_count,
            scene_id=sample.record.scene_id,
        )
        heatmaps = bundle.heatmaps
        if heatmaps.ndim != 3 or heatmaps.shape[0] != sample.record.num_keypoints:
            raise BusinessError(
                "E_CHANNEL_MISMATCH",
                "heatmap observation channel count does not match num_keypoints",
                field="num_keypoints",
                scene_id=sample.record.scene_id,
                details={
                    "heatmap_channels": int(heatmaps.shape[0]) if heatmaps.ndim else 0,
                    "num_keypoints": sample.record.num_keypoints,
                },
            )
        if tuple(heatmaps.shape[1:]) != tuple(sample.record.canvas_shape[:2]):
            raise BusinessError(
                "E_CANVAS_INVALID",
                "heatmap observation spatial shape does not match canvas_shape",
                field="canvas_shape",
                scene_id=sample.record.scene_id,
                details={
                    "heatmap_shape": list(heatmaps.shape),
                    "canvas_shape": list(sample.record.canvas_shape),
                },
            )
        self._set_heatmaps(heatmaps, bundle.heatmap_record.checksum)

    def _set_heatmaps(self, array: np.ndarray, checksum: str) -> None:
        if checksum == self._heatmaps_checksum and self._heatmaps is not None:
            return
        self._heatmaps = np.ascontiguousarray(array, dtype=np.float32)
        self._heatmaps_checksum = checksum

    @property
    def heatmaps(self) -> np.ndarray:
        assert self._heatmaps is not None
        return self._heatmaps

    @property
    def scene_id(self) -> str:
        return self.sample.record.scene_id

    @property
    def score_decimals(self) -> int:
        return int(self.bundle.scoring_config.get("score_decimals", 9))

    def _heatmap_checksum(self) -> str:
        return self.bundle.heatmap_record.checksum

    def _full_peaks_key(self) -> str:
        return make_cache_key(
            "peaks",
            self.scene_id,
            "full",
            self._heatmap_checksum(),
            self.config.quantization_decimals,
            self.config.refine_radius,
            self.config.peak_threshold,
            self.config.peak_neighborhood_radius,
        )

    def _region_peaks_key(self, region: RegionSpec, crop: tuple[int, int, int, int]) -> str:
        return make_cache_key(
            "peaks",
            self.scene_id,
            region.region_id,
            crop,
            self._heatmap_checksum(),
            self.config.quantization_decimals,
            self.config.refine_radius,
            self.config.peak_threshold,
            self.config.peak_neighborhood_radius,
        )

    def _group_key(self, peaks_key: str) -> str:
        return make_cache_key(
            "group",
            self.scene_id,
            peaks_key,
            self.bundle.metadata_record.checksum,
        )

    def _score_key(self, group_key: str) -> str:
        return make_cache_key(
            "score",
            self.scene_id,
            group_key,
            self.bundle.scoring_record.checksum,
        )

    def _peaks_for_full(self) -> tuple[list[Peak], str]:
        key = self._full_peaks_key()
        cached = self.cache.get(key, "peaks")
        if cached is not None:
            return cached, key
        peaks = detect_and_refine(
            self.heatmaps,
            self.sample.record.num_keypoints,
            self.config,
            scene_id=self.scene_id,
        )
        self.cache.put(key, "peaks", peaks)
        return peaks, key

    def _group(self, peaks: list[Peak], peaks_key: str) -> tuple[list[Instance], str]:
        key = self._group_key(peaks_key)
        cached = self.cache.get(key, "group")
        if cached is not None:
            return cached, key
        instances = group_instances(
            peaks,
            self.bundle.metadata,
            scene_id=self.scene_id,
        )
        self.cache.put(key, "group", instances)
        return instances, key

    def _score(
        self, group_instances_value: list[Instance], group_key: str
    ) -> tuple[list[Instance], str]:
        key = self._score_key(group_key)
        cached = self.cache.get(key, "score")
        if cached is not None:
            return cached, key
        instances = copy.deepcopy(group_instances_value)
        scored = score_instances(
            instances,
            self.bundle.metadata,
            self.bundle.scoring_config,
            scene_id=self.scene_id,
        )
        self.cache.put(key, "score", scored)
        return scored, key

    def _attach_provenance(
        self,
        instances: list[Instance],
        node_path: list[str],
        score_key: str,
    ) -> None:
        for instance in instances:
            instance.cache_key = score_key
            instance.snapshot_versions = dict(self.bundle.versions)
            instance.snapshot_checksums = dict(self.bundle.checksums)
            instance.node_path = list(node_path)

    def run_full(self) -> BatchResult:
        start = time.perf_counter_ns()
        before_stats = self.cache.stats()
        peaks, peaks_key = self._peaks_for_full()
        grouped, group_key = self._group(peaks, peaks_key)
        scored, score_key = self._score(grouped, group_key)
        node_path = [peaks_key, group_key, score_key]
        self._attach_provenance(scored, node_path, score_key)
        metrics = scene_metrics(
            scored,
            score_decimals=self.score_decimals,
            extra={"mode": "batch"},
        )
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000.0
        return BatchResult(
            scene_id=self.scene_id,
            instances=scored,
            peaks=peaks,
            metrics=metrics,
            elapsed_ms=elapsed_ms,
            snapshot_versions=dict(self.bundle.versions),
            snapshot_checksums=dict(self.bundle.checksums),
            node_path=node_path,
            cache_key=score_key,
            cache_stats=self.cache.delta(before_stats),
        )

    def region_spec(self, row: int, col: int) -> RegionSpec:
        x0, y0, x1, y1 = self.config.region_bbox(
            row, col, self.sample.record.canvas_shape
        )
        return RegionSpec(row, col, x0, y0, x1, y1)

    def _crop_bounds(self, region: RegionSpec) -> tuple[int, int, int, int]:
        height, width = self.heatmaps.shape[1:]
        halo = int(
            math.ceil(
                float(self.bundle.metadata["max_instance_diameter"])
                + self.config.refine_radius
                + self.config.peak_neighborhood_radius
                + 2
            )
        )
        return (
            max(0, region.x0 - halo),
            max(0, region.y0 - halo),
            min(width, region.x1 + halo),
            min(height, region.y1 + halo),
        )

    def _peaks_for_region(
        self, region: RegionSpec
    ) -> tuple[list[Peak], str, tuple[int, int, int, int]]:
        crop = self._crop_bounds(region)
        key = self._region_peaks_key(region, crop)
        cached = self.cache.get(key, "peaks")
        if cached is not None:
            return cached, key, crop
        x0, y0, x1, y1 = crop
        cropped = self.heatmaps[:, y0:y1, x0:x1]
        full_scene = (x0 == 0 and y0 == 0 and x1 == self.heatmaps.shape[2] and y1 == self.heatmaps.shape[1])
        peaks = detect_and_refine(
            cropped,
            self.sample.record.num_keypoints,
            self.config,
            offset=(x0, y0),
            exclude_edge_margin=0 if full_scene else self.config.peak_neighborhood_radius,
            scene_id=self.scene_id,
        )
        self.cache.put(key, "peaks", peaks)
        return peaks, key, crop

    def run_region(
        self,
        row: int,
        col: int,
        *,
        latency_budget_ms: float | None = None,
        completed_regions: list[str] | None = None,
    ) -> RegionResult:
        region = self.region_spec(row, col)
        budget_ms = (
            self.config.latency_budget_ms
            if latency_budget_ms is None
            else float(latency_budget_ms)
        )
        start = time.perf_counter_ns()
        before_stats = self.cache.stats()
        crop_peaks, peaks_key, crop = self._peaks_for_region(region)
        grouped, group_key = self._group(crop_peaks, peaks_key)
        core_instances = filter_instances_for_region(grouped, region)
        scored, score_key = self._score(core_instances, group_key)
        core_peaks = filter_peaks_to_region(crop_peaks, region)
        node_path = [peaks_key, group_key, score_key]
        self._attach_provenance(scored, node_path, score_key)
        elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000.0
        x0, y0, x1, y1 = crop
        window_cells = (2 * self.config.refine_radius + 1) ** 2
        estimate = estimate_region_latency(
            core_pixels=region.pixel_count,
            expanded_pixels=(x1 - x0) * (y1 - y0),
            peak_candidates=len(crop_peaks),
            refine_window_cells=len(crop_peaks) * window_cells,
        )
        if elapsed_ms > budget_ms:
            raise BusinessError(
                "E_LATENCY_BUDGET",
                "region first-query latency exceeded the configured budget",
                field="latency_budget_ms",
                scene_id=self.scene_id,
                details={
                    "region_id": region.region_id,
                    "observed_ms": round(elapsed_ms, 6),
                    "budget_ms": budget_ms,
                    "estimate": estimate,
                    "completed_regions": list(completed_regions or []),
                },
            )
        metrics = scene_metrics(
            scored,
            score_decimals=self.score_decimals,
            extra={
                "mode": "region_preview",
                "region_id": region.region_id,
                "core_peak_count": len(core_peaks),
            },
        )
        return RegionResult(
            scene_id=self.scene_id,
            region=region,
            peaks=core_peaks,
            instances=scored,
            metrics=metrics,
            elapsed_ms=elapsed_ms,
            estimate=estimate,
            snapshot_versions=dict(self.bundle.versions),
            snapshot_checksums=dict(self.bundle.checksums),
            node_path=node_path,
            cache_key=score_key,
            cache_stats=self.cache.delta(before_stats),
        )

    def decode_crop(self, region: RegionSpec) -> tuple[np.ndarray, tuple[int, int]]:
        x0, y0, x1, y1 = self._crop_bounds(region)
        return self.heatmaps[:, y0:y1, x0:x1], (x0, y0)

    def instance_canonical_bytes(self, instances: list[Instance]) -> bytes:
        return canonical_json_bytes(
            [instance.canonical(self.score_decimals) for instance in instances]
        )


def estimate_region_latency(
    *,
    core_pixels: int,
    expanded_pixels: int,
    peak_candidates: int,
    refine_window_cells: int,
) -> dict[str, Any]:
    # The estimate is a deterministic, inspectable cost model. Budget enforcement
    # always uses the measured perf_counter_ns interval, never this estimate.
    pixel_cost_ns = 30.0
    peak_cost_ns = 80_000.0
    refine_cell_cost_ns = 120.0
    estimated_ns = (
        expanded_pixels * pixel_cost_ns
        + peak_candidates * peak_cost_ns
        + refine_window_cells * refine_cell_cost_ns
    )
    return {
        "model": "pixels+peaks+refine_cells_v1",
        "core_pixels": core_pixels,
        "expanded_pixels": expanded_pixels,
        "peak_candidates": peak_candidates,
        "refine_window_cells": refine_window_cells,
        "unit_costs_ns": {
            "expanded_pixel": pixel_cost_ns,
            "peak_candidate": peak_cost_ns,
            "refine_cell": refine_cell_cost_ns,
        },
        "estimated_ms": round(estimated_ns / 1_000_000.0, 6),
    }

