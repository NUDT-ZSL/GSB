from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable

import numpy as np

from .config import CLASS_IDS, SegmentationConfig, load_config
from .errors import (
    E_INDEX_INVALID,
    E_RECOMPUTE_BUDGET_PENDING,
    E_STATE_INVALID,
    E_TILE_SHAPE_MISMATCH,
    Failure,
    InputError,
    StagedError,
    sort_failures,
)
from .features import extract_and_classify
from .fusion import (
    RegionPlan,
    build_region_plans,
    fuse_region,
    postprocess_classes,
    region_dependency,
)
from .index import IndexData, load_index, validate_image_header
from .model import FusionResult, TileMeta, TileResult
from .overlay import OverlayData, TilePatch, load_overlay
from .state import Snapshot, StateStore
from .tiles import decode_tile, source_fingerprint, tile_cache_key
from .util import atomic_write_json, read_json, utc_now


@dataclass
class PipelineResult:
    workspace: Path
    state_dir: Path
    business_codes: list[str]
    converged: bool
    exit_code: int
    snapshot_version: int
    global_classes: np.ndarray
    tile_results: dict[str, TileResult]
    report: dict[str, Any]
    manifest: dict[str, Any]

    @property
    def scores(self) -> dict[str, np.ndarray]:
        return {key: result.scores for key, result in self.tile_results.items()}


@dataclass
class _TilePlan:
    meta: TileMeta
    patches: tuple[TilePatch, ...]
    band_fingerprint: str
    cache_key: str


@dataclass
class _FusionEntry:
    dep_fingerprint: str
    bounds: tuple[int, int, int, int]
    result_version: int


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _empty_global_bounds(metas: list[TileMeta]) -> tuple[int, int, int, int]:
    if not metas:
        return 0, 0, 0, 0
    return (
        min(meta.bounds[0] for meta in metas),
        min(meta.bounds[1] for meta in metas),
        max(meta.bounds[2] for meta in metas),
        max(meta.bounds[3] for meta in metas),
    )


def _bounds_shape(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    return bounds[3] - bounds[1], bounds[2] - bounds[0]


class PipelineEngine:
    def __init__(
        self,
        workspace: str | Path,
        state_dir: str | Path,
        *,
        config_path: str | Path | None = None,
        index_path: str | Path | None = None,
        overlay_path: str | Path | None = None,
        mode: str = "incremental",
        max_batches: int = 1,
        stop_event: Event | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.config_path = Path(config_path).resolve() if config_path else self.workspace / "config.json"
        self.index_path = Path(index_path).resolve() if index_path else self.workspace / "tiles.json"
        if overlay_path is None:
            candidate = self.workspace / "overlay.json"
            self.overlay_path = candidate if candidate.exists() else None
        else:
            self.overlay_path = Path(overlay_path).resolve()
        if mode not in ("incremental", "full"):
            raise ValueError("mode must be 'incremental' or 'full'")
        if max_batches < 0:
            raise ValueError("max_batches must be non-negative")
        self.mode = mode
        self.max_batches = max_batches
        self.stop_event = stop_event or Event()
        self.progress = progress

    def run(self) -> PipelineResult:
        run_start = time.perf_counter()
        timings: dict[str, float] = {
            "index_validation": 0.0,
            "tile_validation": 0.0,
            "fingerprint": 0.0,
            "feature_extraction": 0.0,
            "classification": 0.0,
            "fusion": 0.0,
            "postprocess": 0.0,
            "snapshot": 0.0,
        }

        stage = time.perf_counter()
        config: SegmentationConfig = load_config(self.config_path)
        timings["index_validation"] += _elapsed_ms(stage)

        stage = time.perf_counter()
        index: IndexData = load_index(self.index_path)
        failures: list[Failure] = list(index.failures)
        timings["index_validation"] += _elapsed_ms(stage)

        store = StateStore(self.state_dir)
        failures.extend(store.recovery_failures)
        previous = None if self.mode == "full" else store.load_snapshot()
        # A corrupt old snapshot is diagnosed but must not prevent rebuilding.
        if self.mode == "full":
            previous = None

        stage = time.perf_counter()
        valid_metas: list[TileMeta] = []
        for meta in sorted(index.tiles, key=lambda item: item.stable_key):
            failure = validate_image_header(meta)
            if failure is not None:
                failures.append(failure)
            else:
                valid_metas.append(meta)
        timings["tile_validation"] += _elapsed_ms(stage)

        stage = time.perf_counter()
        overlay: OverlayData = load_overlay(self.overlay_path, valid_metas)
        failures.extend(overlay.failures)
        plans: dict[str, _TilePlan] = {}
        for meta in valid_metas:
            patches = overlay.patches_by_tile.get(meta.stable_key, ())
            try:
                band_fingerprint = source_fingerprint(meta, patches)
            except OSError as exc:
                failures.append(
                    Failure(
                        E_TILE_SHAPE_MISMATCH,
                        "load",
                        f"cannot read tile content: {exc}",
                        meta.row,
                        meta.col,
                    )
                )
                continue
            cache_key = tile_cache_key(meta, band_fingerprint, config.classification_fingerprint)
            plans[meta.id] = _TilePlan(meta, patches, band_fingerprint, cache_key)
        timings["fingerprint"] += _elapsed_ms(stage)

        previous_manifest = previous.manifest if previous else {}
        previous_tiles = previous_manifest.get("tiles", {}) if isinstance(previous_manifest, dict) else {}
        previous_fusion = previous_manifest.get("fusion", {}) if isinstance(previous_manifest, dict) else {}

        results: dict[str, TileResult] = {}
        reused_ids: set[str] = set()
        for tile_id, plan in sorted(plans.items()):
            if self.mode == "full":
                continue
            entry = previous_tiles.get(tile_id)
            if not isinstance(entry, dict):
                continue
            old_key = entry.get("cache_key")
            if not isinstance(old_key, str) or not old_key:
                continue
            result = store.load_tile_result(plan.meta.row, plan.meta.col, old_key)
            if result is None or result.classes.shape != plan.meta.expected_shape:
                continue
            result.result_version = int(entry.get("result_version", 0))
            results[tile_id] = result
            if old_key == plan.cache_key:
                reused_ids.add(tile_id)

        region_plans = build_region_plans(valid_metas)
        target_tile_keys = {tile_id: plan.cache_key for tile_id, plan in plans.items()}
        target_region_deps: dict[str, str] = {}
        for region_id, region in region_plans.items():
            if all(meta.id in target_tile_keys for meta in region.participants):
                target_region_deps[region_id] = region_dependency(
                    region, target_tile_keys, config.fusion_strategy
                )

        fusion_entries: dict[str, _FusionEntry] = {}
        for region_id, region in region_plans.items():
            entry = previous_fusion.get(region_id)
            if not isinstance(entry, dict):
                continue
            dep = entry.get("dep_fingerprint")
            if isinstance(dep, str) and dep:
                fusion_entries[region_id] = _FusionEntry(
                    dep_fingerprint=dep,
                    bounds=tuple(int(v) for v in entry.get("bounds", region.bounds)),  # type: ignore[arg-type]
                    result_version=int(entry.get("result_version", 0)),
                )

        batches: list[dict[str, Any]] = []
        attempted_tiles: set[str] = set()
        stopped = False
        snapshot_version = previous.version if previous else 0
        global_bounds = _empty_global_bounds(valid_metas)
        global_array = np.zeros(_bounds_shape(global_bounds), dtype=np.uint8)
        final_manifest: dict[str, Any] = {}
        final_report: dict[str, Any] = {}
        snapshot_path = store.snapshot_dir
        snapshot_digest = ""

        batch_index = 0
        while True:
            if self.stop_event.is_set():
                stopped = True
                break
            batch_index += 1
            batch_start = time.perf_counter()

            pending_tiles = [
                plan
                for tile_id, plan in sorted(plans.items())
                if tile_id not in results or results[tile_id].cache_key != plan.cache_key
            ]
            invalid_region_ids: list[str] = []
            eligible_region_ids: list[str] = []
            for region_id, region in sorted(region_plans.items()):
                dep = target_region_deps.get(region_id)
                entry = fusion_entries.get(region_id)
                present = bool(
                    entry
                    and dep
                    and entry.dep_fingerprint == dep
                    and store.fusion_cache_exists(region.row, region.col, dep)
                )
                if not present:
                    invalid_region_ids.append(region_id)
                if (
                    not present
                    and dep is not None
                    and all(
                        meta.id in results and results[meta.id].cache_key == target_tile_keys[meta.id]
                        for meta in region.participants
                    )
                ):
                    eligible_region_ids.append(region_id)

            reusable_tile_count = len(plans) - len(pending_tiles)
            unit_candidates: list[tuple[tuple[int, int, int], str, Any]] = []
            for plan in pending_tiles:
                if plan.meta.id in attempted_tiles:
                    continue
                unit_candidates.append(((plan.meta.row, plan.meta.col, 0), "tile", plan))
            for region_id in eligible_region_ids:
                region = region_plans[region_id]
                unit_candidates.append(((region.row, region.col, 1), "fusion", region))
            unit_candidates.sort(key=lambda item: item[0])

            if self.mode == "full":
                selected_units = unit_candidates
            else:
                selected_units = unit_candidates[: config.recompute_budget]
            selected_tiles = [item[2] for item in selected_units if item[1] == "tile"]
            selected_regions = [item[2] for item in selected_units if item[1] == "fusion"]
            if self.mode == "full":
                # Full runs ignore the local budget and perform all currently
                # eligible fusion regions after tile workers finish.
                selected_regions = list(region_plans.values())

            recomputed_ids: list[str] = []
            batch_failures: list[Failure] = []
            successful_tasks = 0
            batch_feature_ms = 0.0
            batch_classification_ms = 0.0
            if selected_tiles:
                attempts = [(plan.meta.id, plan) for plan in selected_tiles]
                for tile_id, _ in attempts:
                    attempted_tiles.add(tile_id)
                with ThreadPoolExecutor(max_workers=max(1, config.concurrency)) as executor:
                    futures = [
                        (
                            plan,
                            executor.submit(
                                self._process_tile,
                                plan,
                                config,
                            ),
                        )
                        for plan in selected_tiles
                    ]
                    for plan, future in futures:
                        try:
                            result, feature_ms, classify_ms = future.result()
                            result.result_version = self._next_tile_version(
                                tile_id=plan.meta.id,
                                cache_key=plan.cache_key,
                                previous_tiles=previous_tiles,
                            )
                            store.write_tile_result(result)
                            results[plan.meta.id] = result
                            recomputed_ids.append(plan.meta.id)
                            successful_tasks += 1
                            batch_feature_ms += feature_ms
                            batch_classification_ms += classify_ms
                        except StagedError as exc:
                            batch_failures.append(
                                Failure(exc.code, exc.stage, exc.message, plan.meta.row, plan.meta.col)
                            )
                        except Exception as exc:
                            batch_failures.append(
                                Failure(
                                    E_TILE_SHAPE_MISMATCH,
                                    "classification",
                                    f"{type(exc).__name__}: {exc}",
                                    plan.meta.row,
                                    plan.meta.col,
                                )
                            )
            timings["feature_extraction"] += batch_feature_ms
            timings["classification"] += batch_classification_ms
            failures.extend(batch_failures)

            if self.stop_event.is_set():
                stopped = True

            recomputed_regions: list[str] = []
            fusion_time_start = time.perf_counter()
            if not stopped:
                seen_regions: set[str] = set()
                for region in sorted(selected_regions, key=lambda item: (item.row, item.col)):
                    region_id = region.id
                    if region_id in seen_regions:
                        continue
                    seen_regions.add(region_id)
                    dep = target_region_deps.get(region_id)
                    if dep is None:
                        continue
                    if not all(
                        meta.id in results and results[meta.id].cache_key == target_tile_keys[meta.id]
                        for meta in region.participants
                    ):
                        continue
                    old_entry = fusion_entries.get(region_id)
                    if (
                        old_entry
                        and old_entry.dep_fingerprint == dep
                        and store.fusion_cache_exists(region.row, region.col, dep)
                    ):
                        continue
                    fused = fuse_region(region, results, config, dep)
                    fused.result_version = self._next_fusion_version(
                        region_id, dep, previous_fusion, fusion_entries
                    )
                    store.write_fusion_result(fused)
                    fusion_entries[region_id] = _FusionEntry(dep, region.bounds, fused.result_version)
                    recomputed_regions.append(region_id)
            timings["fusion"] += _elapsed_ms(fusion_time_start)

            pending_tiles_after = [
                tile_id
                for tile_id, plan in sorted(plans.items())
                if tile_id not in results or results[tile_id].cache_key != plan.cache_key
            ]
            pending_fusion_after: list[str] = []
            for region_id, region in sorted(region_plans.items()):
                dep = target_region_deps.get(region_id)
                entry = fusion_entries.get(region_id)
                present = bool(
                    entry
                    and dep
                    and entry.dep_fingerprint == dep
                    and store.fusion_cache_exists(region.row, region.col, dep)
                )
                if not present:
                    pending_fusion_after.append(region_id)

            hard_failures = any(
                failure.code in (E_INDEX_INVALID, E_TILE_SHAPE_MISMATCH) for failure in failures
            )
            pending_any = bool(pending_tiles_after or pending_fusion_after)
            converged = bool(valid_metas) and not pending_any and not hard_failures
            if converged:
                status = "converged"
            elif stopped:
                status = "interrupted"
            elif hard_failures:
                status = "failed"
            else:
                status = "pending"

            assembly_start = time.perf_counter()
            global_array, current_region_count, stale_region_count = self._assemble_global(
                store,
                plans,
                region_plans,
                fusion_entries,
                target_region_deps,
                global_bounds,
            )
            timings["postprocess"] += _elapsed_ms(assembly_start)
            post_start = time.perf_counter()
            postprocessed, small_components = postprocess_classes(global_array, config.min_connected_area)
            timings["postprocess"] += _elapsed_ms(post_start)
            global_array = postprocessed
            area_stats = {
                str(class_id): int(np.count_nonzero(global_array == class_id))
                for class_id in CLASS_IDS
            }

            codes: set[str] = {failure.code for failure in failures}
            if pending_any:
                codes.add(E_RECOMPUTE_BUDGET_PENDING)
            business_codes = sorted(codes)
            snapshot_version += 1
            manifest = self._build_manifest(
                snapshot_version=snapshot_version,
                mode=self.mode,
                config=config,
                index=index,
                plans=plans,
                results=results,
                region_plans=region_plans,
                fusion_entries=fusion_entries,
                target_region_deps=target_region_deps,
                global_bounds=global_bounds,
                converged=converged,
                business_codes=business_codes,
                pending_tiles=pending_tiles_after,
                pending_fusion=pending_fusion_after,
            )

            batch_report = {
                "batch_index": batch_index,
                "snapshot_version": snapshot_version,
                "status": status,
                "invalid_tiles": [plan.meta.id for plan in pending_tiles],
                "invalid_tile_count": len(pending_tiles),
                "reused_tiles": reusable_tile_count,
                "reused_tile_count": reusable_tile_count,
                "recomputed_tiles": sorted(recomputed_ids),
                "recomputed_tile_count": len(recomputed_ids),
                "invalid_fusion_regions": sorted(invalid_region_ids),
                "invalid_fusion_region_count": len(invalid_region_ids),
                "recomputed_fusion_regions": sorted(recomputed_regions),
                "recomputed_fusion_region_count": len(recomputed_regions),
                "current_fusion_regions": current_region_count,
                "stale_fusion_regions": stale_region_count,
                "feature_extractions": successful_tasks,
                "pending_tiles": pending_tiles_after,
                "pending_fusion_regions": pending_fusion_after,
                "small_components_replaced": small_components,
                "timings_ms": {
                    "tile_workers_feature_sum": round(batch_feature_ms, 3),
                    "tile_workers_classification_sum": round(batch_classification_ms, 3),
                    "fusion": round(_elapsed_ms(fusion_time_start), 3),
                },
            }
            batches.append(batch_report)

            totals = self._build_totals(results, plans, fusion_entries, region_plans, target_region_deps, store)
            final_report = {
                "schema_version": 1,
                "run_mode": self.mode,
                "status": status,
                "converged": converged,
                "business_codes": business_codes,
                "snapshot_version": snapshot_version,
                "config_hash": config.config_hash,
                "classification_fingerprint": config.classification_fingerprint,
                "index_hash": index.index_hash,
                "index_version": index.index_version,
                "band_version": index.band_version,
                "global_bounds": list(global_bounds),
                "tile_counts": totals,
                "batches": batches,
                "unconverged": {
                    "tiles": pending_tiles_after,
                    "fusion_regions": pending_fusion_after,
                },
                "timings_ms": {key: round(value, 3) for key, value in timings.items()},
                "area_stats": area_stats,
                "failures": [failure.as_dict() for failure in sort_failures(failures)],
                "generated_at": utc_now(),
            }
            final_manifest = manifest
            snapshot_start = time.perf_counter()
            snapshot_path, snapshot_digest = store.write_snapshot(
                manifest,
                global_array,
                final_report,
                results,
            )
            timings["snapshot"] += _elapsed_ms(snapshot_start)
            batch_report["snapshot_path"] = str(snapshot_path)
            batch_report["snapshot_manifest_sha256"] = snapshot_digest
            final_report["snapshot_path"] = str(snapshot_path)
            final_report["snapshot_manifest_sha256"] = snapshot_digest
            final_report["timings_ms"] = {key: round(value, 3) for key, value in timings.items()}
            atomic_write_json(store.root / "diagnostics.json", final_report)

            if self.progress is not None:
                self.progress(batch_report)

            if stopped:
                break
            if not pending_any:
                break
            if self.mode == "full":
                break
            if self.max_batches and batch_index >= self.max_batches:
                break
            if not selected_tiles and not selected_regions:
                break

        if not batches:
            # This is only reachable when an external stop event was already set.
            stopped = True
            pending_tiles = [
                tile_id
                for tile_id, plan in sorted(plans.items())
                if tile_id not in results or results[tile_id].cache_key != plan.cache_key
            ]
            pending_fusion = [
                region_id
                for region_id, region in sorted(region_plans.items())
                if not (
                    (entry := fusion_entries.get(region_id))
                    and entry.dep_fingerprint == target_region_deps.get(region_id)
                    and store.fusion_cache_exists(region.row, region.col, entry.dep_fingerprint)
                )
            ]
            business_codes = sorted({failure.code for failure in failures} | {E_RECOMPUTE_BUDGET_PENDING})
            snapshot_version += 1
            final_manifest = self._build_manifest(
                snapshot_version=snapshot_version,
                mode=self.mode,
                config=config,
                index=index,
                plans=plans,
                results=results,
                region_plans=region_plans,
                fusion_entries=fusion_entries,
                target_region_deps=target_region_deps,
                global_bounds=global_bounds,
                converged=False,
                business_codes=business_codes,
                pending_tiles=pending_tiles,
                pending_fusion=pending_fusion,
            )
            final_report = {
                "schema_version": 1,
                "run_mode": self.mode,
                "status": "interrupted",
                "converged": False,
                "business_codes": business_codes,
                "snapshot_version": snapshot_version,
                "config_hash": config.config_hash,
                "classification_fingerprint": config.classification_fingerprint,
                "index_hash": index.index_hash,
                "index_version": index.index_version,
                "band_version": index.band_version,
                "global_bounds": list(global_bounds),
                "tile_counts": self._build_totals(
                    results, plans, fusion_entries, region_plans, target_region_deps, store
                ),
                "batches": [],
                "unconverged": {"tiles": pending_tiles, "fusion_regions": pending_fusion},
                "timings_ms": {key: round(value, 3) for key, value in timings.items()},
                "area_stats": {
                    str(class_id): int(np.count_nonzero(global_array == class_id)) for class_id in CLASS_IDS
                },
                "failures": [failure.as_dict() for failure in sort_failures(failures)],
                "generated_at": utc_now(),
            }
            snapshot_path, snapshot_digest = store.write_snapshot(
                final_manifest, global_array, final_report, results
            )
            atomic_write_json(store.root / "diagnostics.json", final_report)

        timings["total"] = _elapsed_ms(run_start)
        final_report["timings_ms"] = {key: round(value, 3) for key, value in timings.items()}
        final_report["snapshot_path"] = str(snapshot_path)
        final_report["snapshot_manifest_sha256"] = snapshot_digest
        atomic_write_json(store.root / "diagnostics.json", final_report)
        final_manifest = read_json(snapshot_path / "manifest.json")

        codes = list(final_report.get("business_codes", []))
        return PipelineResult(
            workspace=self.workspace,
            state_dir=self.state_dir,
            business_codes=codes,
            converged=bool(final_report.get("converged", False)),
            exit_code=0,
            snapshot_version=int(final_report.get("snapshot_version", 0)),
            global_classes=global_array,
            tile_results=results,
            report=final_report,
            manifest=final_manifest,
        )

    def _process_tile(
        self,
        plan: _TilePlan,
        config: SegmentationConfig,
    ) -> tuple[TileResult, float, float]:
        tile = decode_tile(plan.meta, plan.patches)
        feature_start = time.perf_counter()
        try:
            result = extract_and_classify(tile, config)
        except Exception as exc:
            if isinstance(exc, StagedError):
                raise
            raise StagedError("feature_extraction", f"{type(exc).__name__}: {exc}") from exc
        feature_ms = _elapsed_ms(feature_start)
        result.cache_key = plan.cache_key
        return result, feature_ms, 0.0

    @staticmethod
    def _next_tile_version(tile_id: str, cache_key: str, previous_tiles: dict[str, Any]) -> int:
        entry = previous_tiles.get(tile_id) if isinstance(previous_tiles, dict) else None
        if not isinstance(entry, dict):
            return 1
        return int(entry.get("result_version", 0)) + 1

    @staticmethod
    def _next_fusion_version(
        region_id: str,
        dep: str,
        previous_fusion: dict[str, Any],
        current_entries: dict[str, _FusionEntry],
    ) -> int:
        entry = current_entries.get(region_id)
        if entry is not None:
            return entry.result_version + 1
        previous = previous_fusion.get(region_id)
        if isinstance(previous, dict):
            return int(previous.get("result_version", 0)) + 1
        return 1

    def _assemble_global(
        self,
        store: StateStore,
        plans: dict[str, _TilePlan],
        region_plans: dict[str, RegionPlan],
        fusion_entries: dict[str, _FusionEntry],
        target_region_deps: dict[str, str],
        global_bounds: tuple[int, int, int, int],
    ) -> tuple[np.ndarray, int, int]:
        x0, y0, x1, y1 = global_bounds
        output = np.zeros((max(0, y1 - y0), max(0, x1 - x0)), dtype=np.uint8)
        current: list[tuple[RegionPlan, np.ndarray]] = []
        stale: list[tuple[RegionPlan, np.ndarray]] = []
        for region_id, region in sorted(region_plans.items()):
            entry = fusion_entries.get(region_id)
            if entry is None:
                continue
            loaded = store.load_fusion_result(region.row, region.col, entry.dep_fingerprint)
            if loaded is None:
                continue
            expected_shape = _bounds_shape(entry.bounds)
            if loaded.classes.shape != expected_shape:
                continue
            rx0, ry0, rx1, ry1 = entry.bounds
            if rx0 < x0 or ry0 < y0 or rx1 > x1 or ry1 > y1:
                continue
            if entry.dep_fingerprint == target_region_deps.get(region_id):
                current.append((region, loaded.classes))
            else:
                stale.append((region, loaded.classes))
        for region, classes in [*stale, *current]:
            x_start, y_start, x_end, y_end = region.bounds
            output[
                y_start - y0 : y_end - y0,
                x_start - x0 : x_end - x0,
            ] = classes
        return output, len(current), len(stale)

    def _build_totals(
        self,
        results: dict[str, TileResult],
        plans: dict[str, _TilePlan],
        fusion_entries: dict[str, _FusionEntry],
        region_plans: dict[str, RegionPlan],
        target_region_deps: dict[str, str],
        store: StateStore,
    ) -> dict[str, int]:
        current_tiles = sum(
            1
            for tile_id, plan in plans.items()
            if tile_id in results and results[tile_id].cache_key == plan.cache_key
        )
        current_fusion = sum(
            1
            for region_id, region in region_plans.items()
            if (entry := fusion_entries.get(region_id))
            and entry.dep_fingerprint == target_region_deps.get(region_id)
            and store.fusion_cache_exists(region.row, region.col, entry.dep_fingerprint)
        )
        return {
            "planned": len(plans),
            "current": current_tiles,
            "pending": len(plans) - current_tiles,
            "fusion_planned": len(region_plans),
            "fusion_current": current_fusion,
            "fusion_pending": len(region_plans) - current_fusion,
        }

    def _build_manifest(
        self,
        *,
        snapshot_version: int,
        mode: str,
        config: SegmentationConfig,
        index: IndexData,
        plans: dict[str, _TilePlan],
        results: dict[str, TileResult],
        region_plans: dict[str, RegionPlan],
        fusion_entries: dict[str, _FusionEntry],
        target_region_deps: dict[str, str],
        global_bounds: tuple[int, int, int, int],
        converged: bool,
        business_codes: list[str],
        pending_tiles: list[str],
        pending_fusion: list[str],
    ) -> dict[str, Any]:
        tile_entries: dict[str, Any] = {}
        for tile_id, plan in sorted(plans.items()):
            result = results.get(tile_id)
            if result is None:
                continue
            tile_entries[tile_id] = {
                "row": plan.meta.row,
                "col": plan.meta.col,
                "bounds": list(plan.meta.bounds),
                "band_version": plan.meta.band_version,
                "band_order": list(plan.meta.band_order),
                "adjacency_digest": plan.meta.adjacency_digest,
                "band_content_fingerprint": plan.band_fingerprint,
                "cache_key": result.cache_key,
                "desired_cache_key": plan.cache_key,
                "band_digest": result.band_digest,
                "result_version": int(result.result_version),
                "current": result.cache_key == plan.cache_key,
            }
        fusion_manifest: dict[str, Any] = {}
        for region_id, region in sorted(region_plans.items()):
            entry = fusion_entries.get(region_id)
            if entry is None:
                continue
            fusion_manifest[region_id] = {
                "row": region.row,
                "col": region.col,
                "bounds": list(entry.bounds),
                "dep_fingerprint": entry.dep_fingerprint,
                "desired_dep_fingerprint": target_region_deps.get(region_id),
                "result_version": entry.result_version,
                "current": entry.dep_fingerprint == target_region_deps.get(region_id),
            }
        return {
            "schema_version": 1,
            "snapshot_version": snapshot_version,
            "created_at": utc_now(),
            "run_mode": mode,
            "converged": converged,
            "business_codes": business_codes,
            "config_hash": config.config_hash,
            "classification_fingerprint": config.classification_fingerprint,
            "index_hash": index.index_hash,
            "index_version": index.index_version,
            "band_version": index.band_version,
            "fusion_strategy": config.fusion_strategy,
            "min_connected_area": config.min_connected_area,
            "global_bounds": list(global_bounds),
            "tiles": tile_entries,
            "fusion": fusion_manifest,
            "pending": {"tiles": pending_tiles, "fusion_regions": pending_fusion},
        }
