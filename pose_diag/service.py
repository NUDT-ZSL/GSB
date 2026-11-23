"""Application service orchestrating snapshots, cache, previews, and replay."""

from __future__ import annotations

import copy
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from .cache import PipelineCache
from .config import PipelineConfig
from .consistency import compare_region, compare_stitched
from .errors import BusinessError
from .grouping import validate_metadata
from .invalidation import analyze_invalidation
from .io_utils import canonical_json_bytes
from .models import Instance, SceneSample
from .pipeline import BatchResult, PipelineEngine, RegionResult
from .samples import load_manifest, load_scene_sample, validate_scene_record
from .scoring import validate_scoring
from .snapshots import (
    SnapshotBundle,
    SnapshotStore,
    encode_array_payload,
)


class DiagnosticService:
    def __init__(
        self,
        manifest_path: str | Path,
        config: PipelineConfig,
        state_dir: str | Path,
        *,
        reset_state: bool = False,
    ):
        self.manifest_path = Path(manifest_path)
        self.config = config
        self.state_dir = Path(state_dir)
        if reset_state and self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.records = load_manifest(self.manifest_path)
        self.records_by_id = {record.scene_id: record for record in self.records}
        self.cache = PipelineCache()
        self.samples: dict[str, SceneSample] = {}
        self.stores: dict[str, SnapshotStore] = {}
        self.bundles: dict[str, SnapshotBundle] = {}
        self.engines: dict[str, PipelineEngine] = {}
        self.batch_results: dict[str, BatchResult] = {}
        self.region_results: dict[str, dict[str, RegionResult]] = {}
        self.record_history: dict[str, Instance] = {}
        self.last_invalidation: dict[str, Any] | None = None
        self.degradation: dict[str, Any] | None = None

    def scene_ids(self) -> list[str]:
        return [record.scene_id for record in self.records]

    def ensure_scene(self, scene_id: str) -> PipelineEngine:
        if scene_id in self.engines:
            return self.engines[scene_id]
        if scene_id not in self.records_by_id:
            raise BusinessError(
                "E_SCENE_NOT_FOUND",
                "scene_id is not present in the manifest",
                field="scene_id",
                scene_id=scene_id,
            )
        record = self.records_by_id[scene_id]
        validate_scene_record(record, self.config)
        sample = load_scene_sample(self.manifest_path, record, self.config)
        validate_metadata(
            sample.metadata,
            expected_channels=record.num_keypoints,
            expected_persons=record.person_count,
            scene_id=scene_id,
        )
        validate_scoring(sample.scoring, scene_id=scene_id)
        store = SnapshotStore(self.state_dir, scene_id)
        bundle = store.initialize(
            sample.observation_heatmaps,
            sample.metadata,
            sample.scoring,
            expected_channels=record.num_keypoints,
            person_count=record.person_count,
        )
        self.samples[scene_id] = sample
        self.stores[scene_id] = store
        self.bundles[scene_id] = bundle
        engine = PipelineEngine(sample, bundle, self.config, self.cache)
        self.engines[scene_id] = engine
        self.region_results.setdefault(scene_id, {})
        return engine

    def _refresh_engine(self, scene_id: str, bundle: SnapshotBundle) -> PipelineEngine:
        sample = self.samples[scene_id]
        self.bundles[scene_id] = bundle
        engine = PipelineEngine(sample, bundle, self.config, self.cache)
        self.engines[scene_id] = engine
        return engine

    def run_full(self, scene_id: str) -> BatchResult:
        engine = self.ensure_scene(scene_id)
        result = engine.run_full()
        self.batch_results[scene_id] = result
        for instance in result.instances:
            self.record_history[instance.instance_id] = copy.deepcopy(instance)
        return result

    def run_region(
        self,
        scene_id: str,
        row: int,
        col: int,
        *,
        latency_budget_ms: float | None = None,
        completed_regions: list[str] | None = None,
    ) -> RegionResult:
        engine = self.ensure_scene(scene_id)
        result = engine.run_region(
            row,
            col,
            latency_budget_ms=latency_budget_ms,
            completed_regions=completed_regions,
        )
        self.region_results.setdefault(scene_id, {})[result.region.region_id] = result
        for instance in result.instances:
            self.record_history[instance.instance_id] = copy.deepcopy(instance)
        return result

    def preview_center_then_rest(
        self,
        scene_id: str,
        *,
        requested_parallel: int = 1,
        latency_budget_ms: float | None = None,
    ) -> dict[str, Any]:
        self.ensure_scene(scene_id)
        row, col = self.config.center_region
        started = time.perf_counter_ns()
        center = self.run_region(
            scene_id,
            row,
            col,
            latency_budget_ms=latency_budget_ms,
            completed_regions=[],
        )
        first_block_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        all_regions = [
            (r, c)
            for r in range(self.config.region_rows)
            for c in range(self.config.region_cols)
            if (r, c) != (row, col)
        ]
        completed = [center.region.region_id]
        degradation: dict[str, Any] | None = None
        rest: list[RegionResult] = []
        if requested_parallel > self.config.max_parallel_previews:
            degradation = {
                "category": "serial_preview",
                "requested_parallel": requested_parallel,
                "max_parallel_previews": self.config.max_parallel_previews,
                "business_code": "DEGRADED_SERIAL_PREVIEW",
            }
            workers = 1
        else:
            workers = max(1, requested_parallel)
        if workers == 1:
            for index, (r, c) in enumerate(all_regions):
                rest.append(
                    self.run_region(
                        scene_id,
                        r,
                        c,
                        latency_budget_ms=latency_budget_ms,
                        completed_regions=completed + [
                            f"r{rr:02d}c{cc:02d}" for rr, cc in all_regions[:index]
                        ],
                    )
                )
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(
                        self.run_region,
                        scene_id,
                        r,
                        c,
                        latency_budget_ms=latency_budget_ms,
                        completed_regions=list(completed),
                    )
                    for r, c in all_regions
                ]
                for future in futures:
                    rest.append(future.result())
        self.degradation = degradation
        return {
            "business_code": "OK",
            "scene_id": scene_id,
            "center": center,
            "first_block_observed_ms": first_block_ms,
            "first_block_region_elapsed_ms": center.elapsed_ms,
            "first_block_estimate": center.estimate,
            "completed_regions": [
                center.region.region_id,
                *[result.region.region_id for result in rest],
            ],
            "rest": rest,
            "degradation": degradation,
        }

    def compare_scene(self, scene_id: str, preview: dict[str, Any]) -> dict[str, Any]:
        batch = self.batch_results.get(scene_id) or self.run_full(scene_id)
        center: RegionResult = preview["center"]
        rest: list[RegionResult] = preview["rest"]
        per_region = []
        peak_checks: list[dict[str, Any]] = []
        for result in [center, *rest]:
            verdict = compare_region(
                batch.instances,
                result.instances,
                batch.peaks,
                result.peaks,
                result.region,
                score_decimals=_score_decimals(self.bundles[scene_id]),
                scene_id=scene_id,
            )
            per_region.append(verdict)
            if verdict["business_code"] == "E_PEAK_MISMATCH":
                peak_checks.append(verdict)
        stitched = compare_stitched(
            batch.instances,
            [result.instances for result in [center, *rest]],
            score_decimals=_score_decimals(self.bundles[scene_id]),
            scene_id=scene_id,
        )
        failures = [
            verdict
            for verdict in per_region
            if verdict["business_code"] != "OK"
        ]
        overall_ok = stitched["business_code"] == "OK" and not failures
        return {
            "business_code": "OK" if overall_ok else (
                "E_PEAK_MISMATCH"
                if peak_checks
                else "E_BLOCK_MISMATCH"
            ),
            "verdict": "PASS" if overall_ok else "FAIL",
            "scene_id": scene_id,
            "per_region": per_region,
            "stitched": stitched,
            "first_failure": (peak_checks or failures or [None])[0],
        }

    def commit_snapshot(
        self,
        scene_id: str,
        category: str,
        version: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        store = self.stores.get(scene_id)
        if store is None:
            self.ensure_scene(scene_id)
            store = self.stores[scene_id]
        old_bundle = self.bundles[scene_id]
        prior = self.batch_results.get(scene_id)
        prior_instances = prior.instances if prior else []
        store.commit(category, version, payload)
        new_bundle = store.load_bundle()
        plan = analyze_invalidation(
            old_bundle,
            new_bundle,
            self.config,
            prior_instances=prior_instances,
        )
        self._refresh_engine(scene_id, new_bundle)
        self.last_invalidation = plan
        return plan

    def make_scoring_update(
        self, scene_id: str, version: int, *, match_threshold: float | None = None
    ) -> dict[str, Any]:
        self.ensure_scene(scene_id)
        payload = copy.deepcopy(self.bundles[scene_id].scoring_config)
        if match_threshold is not None:
            payload["match_threshold"] = float(match_threshold)
        else:
            payload["match_threshold"] = round(
                float(payload["match_threshold"]) + 0.05, 6
            )
        payload["revision_note"] = f"scoring-v{version}"
        return self.commit_snapshot(scene_id, "scoring", version, payload)

    def make_metadata_correction(
        self, scene_id: str, version: int
    ) -> dict[str, Any]:
        self.ensure_scene(scene_id)
        payload = copy.deepcopy(self.bundles[scene_id].metadata)
        # Correct one person's channel confidence and its first keypoint position
        # metadata. This is intentionally a non-structural, person-local change.
        if payload.get("persons"):
            person = payload["persons"][0]
            if person.get("keypoints"):
                keypoint = person["keypoints"][0]
                keypoint["x"] = round(float(keypoint["x"]) + 0.5, 3)
                keypoint["metadata_fix"] = f"v{version}"
            person["metadata_revision"] = f"v{version}"
        return self.commit_snapshot(scene_id, "keypoint_metadata", version, payload)

    def make_heatmap_update(
        self, scene_id: str, version: int
    ) -> dict[str, Any]:
        self.ensure_scene(scene_id)
        heatmaps = self.bundles[scene_id].heatmaps.copy()
        # A whole-tensor replacement: raise a deterministic observation bump
        # near one corner so peak location and all downstream stages can change.
        height, width = heatmaps.shape[1:]
        y0, y1 = max(0, height // 8 - 5), min(height, height // 8 + 6)
        x0, x1 = max(0, width // 8 - 5), min(width, width // 8 + 6)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        bump = 0.9 * np.exp(
            -((yy - (y0 + y1) / 2.0) ** 2 + (xx - (x0 + x1) / 2.0) ** 2) / 8.0
        )
        heatmaps[0, y0:y1, x0:x1] = np.maximum(
            heatmaps[0, y0:y1, x0:x1], bump.astype(np.float32)
        )
        return self.commit_snapshot(
            scene_id,
            "heatmap_observation",
            version,
            encode_array_payload(heatmaps),
        )

    def load_record(self, instance_id: str) -> Instance:
        if instance_id not in self.record_history:
            raise BusinessError(
                "E_INSTANCE_NOT_FOUND",
                "instance result is not available",
                field="instance_id",
                details={"instance_id": instance_id},
            )
        return copy.deepcopy(self.record_history[instance_id])

    def reject_if_stale(
        self, scene_id: str, instance: Instance
    ) -> dict[str, Any]:
        self.ensure_scene(scene_id)
        current = self.bundles[scene_id]
        mismatches: dict[str, Any] = {}
        for category, recorded_version in instance.snapshot_versions.items():
            if category not in current.records:
                mismatches[category] = {
                    "recorded_version": recorded_version,
                    "current_version": None,
                    "recorded_checksum": instance.snapshot_checksums.get(category),
                    "current_checksum": None,
                }
                continue
            record = current.records[category]
            recorded_checksum = instance.snapshot_checksums.get(category)
            if (
                recorded_version != record.version
                or (
                    recorded_checksum is not None
                    and recorded_checksum != record.checksum
                )
            ):
                mismatches[category] = {
                    "recorded_version": recorded_version,
                    "current_version": record.version,
                    "recorded_checksum": recorded_checksum,
                    "current_checksum": record.checksum,
                }
        if mismatches:
            raise BusinessError(
                "E_STALE_RESULT",
                "cached instance depends on a superseded snapshot version",
                field="snapshot_versions",
                scene_id=scene_id,
                details={
                    "instance_id": instance.instance_id,
                    "cache_key": instance.cache_key,
                    "node_path": list(instance.node_path),
                    "version_mismatches": mismatches,
                    "checksum_mismatches": {
                        category: values
                        for category, values in mismatches.items()
                        if values.get("recorded_checksum")
                        != values.get("current_checksum")
                    },
                },
            )
        return {
            "business_code": "OK",
            "instance_id": instance.instance_id,
            "cache_key": instance.cache_key,
        }

    def replay_instance(self, scene_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.load_record(instance_id)
        if not instance.node_path or len(instance.node_path) < 3:
            raise BusinessError(
                "E_CACHE_MISS",
                "instance result has no replayable node path",
                field="node_path",
                scene_id=scene_id,
                details={"instance_id": instance_id},
            )
        group_key = instance.node_path[1]
        group_value = self.cache.get_value_exact(group_key)
        if group_value is None:
            raise BusinessError(
                "E_CACHE_MISS",
                "upstream grouping node required for replay is unavailable",
                field="cache_key",
                scene_id=scene_id,
                details={"cache_key": group_key},
            )
        store = self.stores[scene_id]
        scoring_record = store.load_version(
            "scoring", int(instance.snapshot_versions["scoring"])
        )
        metadata_record = store.load_version(
            "keypoint_metadata", int(instance.snapshot_versions["keypoint_metadata"])
        )
        from .scoring import score_instances

        replayed = score_instances(
            copy.deepcopy(group_value),
            metadata_record.payload,
            scoring_record.payload,
            scene_id=scene_id,
        )
        original_bytes = canonical_json_bytes(instance.canonical())
        match = next(
            (
                candidate
                for candidate in replayed
                if candidate.instance_id == instance_id
            ),
            None,
        )
        replay_bytes = (
            canonical_json_bytes(match.canonical()) if match is not None else b""
        )
        return {
            "business_code": "OK" if replay_bytes == original_bytes else "E_REPLAY_MISMATCH",
            "instance_id": instance_id,
            "cache_key": instance.cache_key,
            "snapshot_versions": dict(sorted(instance.snapshot_versions.items())),
            "node_path": list(instance.node_path),
            "score_equal": bool(
                match is not None and match.score == instance.score
            ),
            "instance_set_equal": replay_bytes == original_bytes,
        }

    def validation_snapshot(self, scene_id: str) -> dict[str, Any]:
        engine = self.ensure_scene(scene_id)
        bundle = self.bundles[scene_id]
        return {
            "scene_id": scene_id,
            "versions": dict(sorted(bundle.versions.items())),
            "checksums": dict(sorted(bundle.checksums.items())),
            "engine_cache_key": engine._score_key(engine._group_key(engine._full_peaks_key())),
        }


def _score_decimals(bundle: SnapshotBundle) -> int:
    return int(bundle.scoring_config.get("score_decimals", 9))

