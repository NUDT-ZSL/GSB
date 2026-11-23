from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .cache import CacheObject, SliceCache, default_cache_root
from .errors import MatForgeError, fail
from .hashing import combine_digests, digest_obj
from .params import MaterialParams, NoiseParams, changed_leaf_paths
from .pipeline import (
    BlockTexture,
    FullTexture,
    NormalizationStats,
    compute_normalization,
    estimate_block_cost,
    generate_block,
    generate_full,
    semantic_cache_key,
    stitch_blocks,
)
from .plan import BlockPlan
from .snapshot import Snapshot, SnapshotStore


@dataclass(frozen=True)
class ResourceLimits:
    max_material_pixels: int = 8_388_608
    max_block_resident_bytes: int = 32_000_000
    max_parallel_slices: int = 4
    preview_latency_budget_ms: float = 350.0

    def validate(self) -> None:
        if self.max_material_pixels < 1 or self.max_block_resident_bytes < 1:
            fail("E_PARAM_INVALID", "资源上限必须为正数")
        if not 1 <= self.max_parallel_slices <= 64:
            fail("E_PARAM_INVALID", "max_parallel_slices 必须在 1..64 之间")
        if self.preview_latency_budget_ms <= 0:
            fail("E_PARAM_INVALID", "preview_latency_budget_ms 必须大于 0")


@dataclass
class ReadySlice:
    block: int
    pixel_range: dict[str, int]
    cache_key: str
    semantic_key: str
    content_hash: str
    channel_digest: str
    seam_digest: str
    upstream_hash: str
    normalization_digest: str
    params_revision: int
    params_digest: str
    plan_version: int
    plan_digest: str
    seed: int
    node_path: str
    duration_ms: float
    source: str
    estimates: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReadySlice:
        fields = cls.__dataclass_fields__
        try:
            return cls(**{key: value[key] for key in fields})
        except KeyError as error:
            fail("E_SNAPSHOT_CORRUPT", "切片清单记录字段不完整", missing=str(error))


def block_resident_estimate(plan: BlockPlan, block_index: int) -> dict[str, Any]:
    block = plan.block(block_index)
    halo_pixels = (block.width + 2) * (block.height + 2)
    per_pixel_bytes = {
        "raw_noise_f64": 8,
        "height_f64": 8,
        "normal_f64": 24,
        "channels_u8": 7,
        "seam_f64": 8,
        "working_overhead": 16,
    }
    total = halo_pixels * sum(per_pixel_bytes.values())
    return {
        "block": block_index,
        "halo_pixels": halo_pixels,
        "per_pixel_bytes": per_pixel_bytes,
        "resident_bytes": total,
    }


class MaterialStudio:
    def __init__(
        self,
        cache_root: str | Path | None = None,
        snapshot_path: str | Path | None = None,
        params: MaterialParams | None = None,
        plan: BlockPlan | None = None,
        limits: ResourceLimits | None = None,
    ):
        self.limits = limits or ResourceLimits()
        self.limits.validate()
        self.cache = SliceCache(cache_root or default_cache_root())
        root = Path(cache_root or default_cache_root())
        self.snapshot_store = SnapshotStore(snapshot_path or root / "snapshot.json")
        self.params = params or MaterialParams()
        self.plan = plan or BlockPlan()
        self.params.validate()
        self._validate_resources(self.plan)
        self.stats = compute_normalization(self.params, self.plan)
        self.params_version = self.params.revision
        self.plan_version = 1
        self.ready_version = 1
        self._state_lock = threading.RLock()
        self._commit_lock = threading.Lock()
        self._preview_slots = threading.BoundedSemaphore(self.limits.max_parallel_slices)
        self._epoch = 0
        self.ready: dict[int, ReadySlice] = {}
        self.superseded: dict[int, ReadySlice] = {}
        self.last_observation: dict[str, Any] | None = None
        self.last_error: dict[str, Any] | None = None
        self.last_invalidation: dict[str, Any] | None = None
        self.degradation: list[str] = []
        self.stage = "initialized"
        if self.snapshot_store.path.exists():
            self.load_snapshot()
        else:
            self._persist_snapshot()

    @property
    def params_digest(self) -> str:
        return self.params.digest()

    @property
    def plan_digest(self) -> str:
        return self.plan.digest()

    def _validate_resources(self, plan: BlockPlan) -> None:
        if plan.pixel_count > self.limits.max_material_pixels:
            fail(
                "E_RESOURCE_LIMIT",
                "材质像素总量超过配置上限",
                category="material_pixels",
                requested=plan.pixel_count,
                limit=self.limits.max_material_pixels,
            )
        for block in plan.blocks():
            estimation = block_resident_estimate(plan, block.index)
            if estimation["resident_bytes"] > self.limits.max_block_resident_bytes:
                fail(
                    "E_RESOURCE_LIMIT",
                    "单块驻留内存估算超过配置上限",
                    category="block_resident_memory",
                    block=block.index,
                    requested=estimation["resident_bytes"],
                    limit=self.limits.max_block_resident_bytes,
                    estimate=estimation,
                )

    def _record_error(self, error: MatForgeError) -> None:
        self.last_error = {**error.to_dict(), "at_stage": self.stage}

    def _snapshot(self) -> Snapshot:
        items = [record.to_dict() for _, record in sorted(self.ready.items())]
        return Snapshot.build(
            params=self.params,
            plan=self.plan,
            plan_version=self.plan_version,
            ready_items=items,
            params_version=self.params_version,
            ready_version=self.ready_version,
        )

    def _persist_snapshot(self) -> None:
        snapshot = self._snapshot()
        self.snapshot_store.save(snapshot)

    def _freshness_error(self, record: ReadySlice) -> MatForgeError | None:
        if (
            record.params_revision != self.params.revision
            or record.params_digest != self.params.digest()
            or record.plan_digest != self.plan.digest()
        ):
            return MatForgeError(
                "E_STALE_SLICE",
                "切片依赖的版本已变化，禁止用于当前整幅结果",
                {
                    "material_id": self.params.material_id,
                    "block": record.block,
                    "stale_version": record.params_revision,
                    "current_version": self.params.revision,
                    "stale_plan_version": record.plan_version,
                    "current_plan_version": self.plan_version,
                    "cache_key": record.cache_key,
                    "node_path": record.node_path,
                },
            )
        return None

    def assert_slice_fresh(self, block_id: int) -> ReadySlice:
        with self._state_lock:
            record = self.ready.get(block_id)
            if record is None:
                if block_id in self.superseded:
                    error = self._freshness_error(self.superseded[block_id])
                    assert error is not None
                    raise error
                fail("E_BLOCK_NOT_READY", "切片尚未生成", block=block_id)
            error = self._freshness_error(record)
            if error is not None:
                raise error
            return record

    def _cache_object_for_ready(self, record: ReadySlice) -> CacheObject:
        self.assert_slice_fresh(record.block)
        obj = self.cache.get(record.cache_key)
        if (
            obj.metadata.get("channel_digest") != record.channel_digest
            or obj.metadata.get("seam_digest") != record.seam_digest
            or obj.metadata.get("params_digest") != record.params_digest
            or obj.metadata.get("params_revision") != record.params_revision
            or obj.metadata.get("plan_digest") != record.plan_digest
        ):
            fail(
                "E_STALE_SLICE",
                "缓存对象来源与切片清单不一致",
                material_id=self.params.material_id,
                block=record.block,
                stale_version=obj.metadata.get("params_revision"),
                current_version=self.params.revision,
                cache_key=record.cache_key,
            )
        return obj

    def _rebind_cached(
        self,
        block_index: int,
        obj: CacheObject,
        params: MaterialParams,
        plan: BlockPlan,
        stats: NormalizationStats,
        source: str = "cache",
    ) -> tuple[BlockTexture, dict[str, Any]]:
        block = plan.block(block_index)
        metadata = obj.metadata
        channels = obj.channels
        seam = obj.seam
        start = time.perf_counter()
        semantic = semantic_cache_key(params, plan, block_index, stats.digest)
        upstream_hash = metadata.get("upstream_hash")
        if not isinstance(upstream_hash, str):
            fail("E_CACHE_CORRUPT", "缓存对象缺少上游摘要", cache_key=obj.cache_key)
        # Recompute the final key using current version metadata and the verified upstream bytes digest.
        cache_key = digest_obj(
            {
                "schema": "matforge.cache-key.v1",
                "params_digest": params.digest(),
                "seed": params.seed,
                "plan_digest": plan.digest(),
                "block_grid": {
                    "width": plan.width,
                    "height": plan.height,
                    "tile_width": plan.tile_width,
                    "tile_height": plan.tile_height,
                    "rows": plan.rows,
                    "cols": plan.cols,
                    "block": block_index,
                },
                "octave_config_digest": params.noise.digest(),
                "normalization_digest": stats.digest,
                "upstream_hash": upstream_hash,
            }
        )
        texture = BlockTexture(
            block=block,
            channels=channels,
            seam=seam,
            raw_noise=np.empty((0, 0), dtype=np.float64),
            normalization_digest=stats.digest,
            upstream_hash=upstream_hash,
            content_digest=combine_digests(metadata["channel_digest"], metadata["seam_digest"]),
            channel_digest=metadata["channel_digest"],
            seam_digest=metadata["seam_digest"],
            duration_ms=(time.perf_counter() - start) * 1000.0,
            stage_timings_ms={"cache_read_ms": (time.perf_counter() - start) * 1000.0},
            estimates=estimate_block_cost(params, plan, block_index),
            cache_key=cache_key,
            semantic_key=semantic,
            node_path=f"material/{params.material_id}/pipeline/channel_map/block/{block_index:04d}",
        )
        cache_record = self.cache.put(texture, params, plan, self.plan_version)
        return texture, cache_record

    def _ensure_block(
        self,
        block_id: int,
        *,
        epoch: int,
        params: MaterialParams,
        plan: BlockPlan,
        stats: NormalizationStats,
        budget_ms: float | None,
        allow_cache: bool = True,
    ) -> dict[str, Any]:
        query_started = time.perf_counter()
        with self._state_lock:
            existing = self.ready.get(block_id)
            if existing is not None:
                error = self._freshness_error(existing)
                if error is not None:
                    raise error
                reuse_ms = (time.perf_counter() - query_started) * 1000.0
                observed = {
                    "block": block_id,
                    "elapsed_ms": reuse_ms,
                    "source": "perf_counter_around_ready_state_query",
                    "source_kind": "state_reuse",
                    "estimate": existing.estimates,
                    "budget_ms": budget_ms,
                    "estimated_over_budget": bool(budget_ms is not None and existing.estimates["estimated_ms"] > budget_ms),
                    "observed_over_budget": bool(budget_ms is not None and reuse_ms > budget_ms),
                    "generation_compute_ms": 0.0,
                }
                result = {
                    "slice": existing.to_dict(),
                    "source": "state_reuse",
                    "observed": observed,
                }
                if budget_ms is not None and reuse_ms > budget_ms:
                    fail(
                        "E_LATENCY_BUDGET",
                        "首块查询耗时超过预算",
                        block=block_id,
                        budget_ms=budget_ms,
                        observed=observed,
                        completed_blocks=sorted(self.ready),
                    )
                return result

        started = query_started
        semantic = semantic_cache_key(params, plan, block_id, stats.digest)
        obj = self.cache.get_by_semantic(semantic) if allow_cache else None
        source = "cache"
        if obj is not None:
            texture, cache_record = self._rebind_cached(block_id, obj, params, plan, stats)
        else:
            self._preview_slots.acquire()
            try:
                texture = generate_block(params, plan, block_id, stats)
                cache_record = self.cache.put(texture, params, plan, self.plan_version)
                source = "generated"
            finally:
                self._preview_slots.release()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        observed = {
            "block": block_id,
            "elapsed_ms": elapsed_ms,
            "source": "perf_counter_around_local_generation_loop",
            "source_kind": source,
            "estimate": texture.estimates,
            "budget_ms": budget_ms,
            "estimated_over_budget": bool(budget_ms is not None and texture.estimates["estimated_ms"] > budget_ms),
            "observed_over_budget": bool(budget_ms is not None and elapsed_ms > budget_ms),
            "generation_compute_ms": texture.duration_ms if source == "generated" else 0.0,
        }
        record = ReadySlice(
            block=block_id,
            pixel_range=texture.block.pixel_range,
            cache_key=texture.cache_key,
            semantic_key=texture.semantic_key,
            content_hash=cache_record["content_hash"],
            channel_digest=texture.channel_digest,
            seam_digest=texture.seam_digest,
            upstream_hash=texture.upstream_hash,
            normalization_digest=texture.normalization_digest,
            params_revision=params.revision,
            params_digest=params.digest(),
            plan_version=self.plan_version,
            plan_digest=plan.digest(),
            seed=params.seed,
            node_path=texture.node_path,
            duration_ms=texture.duration_ms,
            source=source,
            estimates=texture.estimates,
        )
        with self._state_lock:
            if epoch != self._epoch:
                raise MatForgeError(
                    "E_STALE_SLICE",
                    "生成期间参数或计划已替换，结果被拒绝",
                    {
                        "material_id": self.params.material_id,
                        "block": block_id,
                        "stale_version": params.revision,
                        "current_version": self.params.revision,
                        "cache_key": texture.cache_key,
                    },
                )
            previous_record = self.ready.get(block_id)
            previous_ready_version = self.ready_version
            previous_observation = self.last_observation
            previous_stage = self.stage
            self.ready[block_id] = record
            self.ready_version += 1
            self.last_observation = observed
            self.stage = "slice_ready"
            try:
                self._persist_snapshot()
            except Exception:
                if previous_record is None:
                    self.ready.pop(block_id, None)
                else:
                    self.ready[block_id] = previous_record
                self.ready_version = previous_ready_version
                self.last_observation = previous_observation
                self.stage = previous_stage
                raise
            completed = sorted(self.ready)
        result = {"slice": record.to_dict(), "source": source, "observed": observed, "completed_blocks": completed}
        if budget_ms is not None and elapsed_ms > budget_ms:
            fail(
                "E_LATENCY_BUDGET",
                "首块查询耗时超过预算",
                block=block_id,
                budget_ms=budget_ms,
                observed=observed,
                completed_blocks=completed,
                generated_slice=result["slice"],
            )
        return result

    def preview(self, block_id: int = 0, budget_ms: float | None = None) -> dict[str, Any]:
        self.stage = "preview"
        budget = self.limits.preview_latency_budget_ms if budget_ms is None else float(budget_ms)
        with self._state_lock:
            epoch = self._epoch
            params, plan, stats = self.params, self.plan, self.stats
        try:
            result = self._ensure_block(
                block_id,
                epoch=epoch,
                params=params,
                plan=plan,
                stats=stats,
                budget_ms=budget,
            )
            result["progress"] = self.progress()
            return result
        except MatForgeError as error:
            self._record_error(error)
            raise

    def preview_many(
        self,
        block_ids: Iterable[int],
        *,
        parallelism: int | None = None,
        allow_degrade: bool = False,
        budget_ms: float | None = None,
    ) -> dict[str, Any]:
        requested = list(dict.fromkeys(block_ids))
        requested_parallelism = parallelism or min(self.limits.max_parallel_slices, max(1, len(requested)))
        degraded: list[str] = []
        if requested_parallelism > self.limits.max_parallel_slices:
            if not allow_degrade:
                error = MatForgeError(
                    "E_RESOURCE_LIMIT",
                    "并发预览数超过上限",
                    {
                        "category": "parallel_slices",
                        "requested": requested_parallelism,
                        "limit": self.limits.max_parallel_slices,
                    },
                )
                self._record_error(error)
                raise error
            degraded.append("serial_streaming_parallel_cap")
            requested_parallelism = 1
        workers = min(requested_parallelism, max(1, len(requested)))
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def invoke(block_id: int) -> dict[str, Any]:
            return self.preview(block_id, budget_ms=budget_ms)

        if workers == 1:
            for block_id in requested:
                try:
                    results.append(invoke(block_id))
                except MatForgeError as error:
                    errors.append(error.to_dict())
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="matforge-preview") as pool:
                futures = {pool.submit(invoke, block_id): block_id for block_id in requested}
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except MatForgeError as error:
                        errors.append(error.to_dict())
        results.sort(key=lambda item: item["slice"]["block"])
        return {
            "ok": not errors,
            "requested": requested,
            "parallelism": workers,
            "degraded": degraded,
            "results": results,
            "errors": errors,
            "progress": self.progress(),
        }

    def request_remaining(
        self,
        *,
        parallelism: int | None = None,
        allow_degrade: bool = True,
    ) -> dict[str, Any]:
        self.stage = "remaining"
        with self._state_lock:
            epoch = self._epoch
            params, plan, stats = self.params, self.plan, self.stats
            missing = [index for index in range(plan.block_count) if index not in self.ready]
            reused = sorted(index for index in self.ready if index not in missing)
        requested_parallelism = parallelism or min(self.limits.max_parallel_slices, max(1, len(missing)))
        degraded: list[str] = []
        if requested_parallelism > self.limits.max_parallel_slices:
            if not allow_degrade:
                error = MatForgeError(
                    "E_RESOURCE_LIMIT",
                    "并行生成切片数超过上限",
                    {
                        "category": "parallel_slices",
                        "requested": requested_parallelism,
                        "limit": self.limits.max_parallel_slices,
                    },
                )
                self._record_error(error)
                raise error
            degraded.append("streaming_serial_degraded")
            requested_parallelism = 1
        workers = min(requested_parallelism, max(1, len(missing)))
        generated: list[dict[str, Any]] = []
        errors: list[MatForgeError] = []

        def invoke(block_id: int) -> dict[str, Any]:
            return self._ensure_block(
                block_id,
                epoch=epoch,
                params=params,
                plan=plan,
                stats=stats,
                budget_ms=None,
            )

        if workers == 1:
            for block_id in missing:
                try:
                    generated.append(invoke(block_id))
                except MatForgeError as error:
                    errors.append(error)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="matforge-generate") as pool:
                futures = {pool.submit(invoke, block_id): block_id for block_id in missing}
                for future in as_completed(futures):
                    try:
                        generated.append(future.result())
                    except MatForgeError as error:
                        errors.append(error)
        if errors:
            self._record_error(errors[0])
            raise errors[0]
        generated_ids = sorted(item["slice"]["block"] for item in generated)
        with self._state_lock:
            self.stage = "complete" if len(self.ready) == plan.block_count else "partial"
            self.degradation = degraded
        return {
            "ok": True,
            "requested": missing,
            "generated": generated_ids,
            "recomputed": generated_ids,
            "reused": reused,
            "degraded": degraded,
            "parallelism": workers,
            "progress": self.progress(),
        }

    def progress(self) -> dict[str, Any]:
        with self._state_lock:
            total = self.plan.block_count
            ready_count = len(self.ready)
            return {
                "ready": ready_count,
                "total": total,
                "fraction": (ready_count / total) if total else 0.0,
                "complete": ready_count == total,
                "ready_blocks": sorted(self.ready),
                "pending_blocks": [index for index in range(total) if index not in self.ready],
            }

    def _archive_ready(self) -> dict[int, ReadySlice]:
        old = dict(self.ready)
        for index, record in old.items():
            self.superseded[index] = record
        return old

    def commit_plan(
        self,
        params: MaterialParams,
        plan: BlockPlan,
        *,
        generate_suffix: bool = True,
        parallelism: int = 1,
        allow_degrade: bool = False,
    ) -> dict[str, Any]:
        if not self._commit_lock.acquire(blocking=False):
            error = MatForgeError(
                "E_STAGE_BUSY",
                "已有分块计划提交正在生效，后到提交不等待",
                {"stage": "plan_commit", "policy": "non_blocking"},
            )
            self._record_error(error)
            raise error
        try:
            self.stage = "commit"
            params.validate()
            self._validate_resources(plan)
            with self._state_lock:
                current_params = self.params
                current_plan = self.plan
                current_ready = dict(self.ready)
                if params.revision < current_params.revision:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "参数修订版本回退被拒绝",
                        current_revision=current_params.revision,
                        attempted_revision=params.revision,
                    )
                if params.revision == current_params.revision and params.digest() != current_params.digest():
                    fail(
                        "E_REVISION_NOT_MONOTONIC",
                        "参数内容变化时必须提升 revision",
                        current_revision=current_params.revision,
                        incoming_revision=params.revision,
                    )
                changed_paths = changed_leaf_paths(current_params, params)
                plan_changed = plan.digest() != current_plan.digest()
                params_changed = params.digest() != current_params.digest()
                if not params_changed and not plan_changed and params.material_id == current_params.material_id:
                    return {
                        "ok": True,
                        "noop": True,
                        "affected": [],
                        "affected_suffix": [],
                        "recomputed": [],
                        "reused": sorted(current_ready),
                        "changed_paths": [],
                        "versions": self.snapshot_versions(),
                    }
                new_stats = compute_normalization(params, plan)
                semantic_by_block = {
                    index: semantic_cache_key(params, plan, index, new_stats.digest) for index in range(plan.block_count)
                }
                affected = []
                reusable = []
                for index, old_record in sorted(current_ready.items()):
                    if index < plan.block_count and old_record.semantic_key == semantic_by_block[index]:
                        reusable.append(index)
                    else:
                        affected.append(index)
                for index in range(plan.block_count):
                    if index not in current_ready:
                        affected.append(index)
                affected = sorted(set(affected))
                first_affected = min(affected) if affected else plan.block_count
                affected_suffix = list(range(first_affected, plan.block_count))
                # Suffix invalidation intentionally recomputes every block after the first invalid block.
                recompute_targets = [index for index in affected_suffix if index in current_ready or index in affected]
                reuse_targets = [index for index in sorted(current_ready) if index < first_affected]
                prior = {
                    "epoch": self._epoch,
                    "params": self.params,
                    "plan": self.plan,
                    "stats": self.stats,
                    "params_version": self.params_version,
                    "plan_version": self.plan_version,
                    "ready_version": self.ready_version,
                    "ready": dict(self.ready),
                    "superseded": dict(self.superseded),
                    "stage": self.stage,
                    "last_invalidation": self.last_invalidation,
                }
                invalidation = {
                    "changed_paths": changed_paths,
                    "plan_changed": plan_changed,
                    "affected": affected,
                    "affected_suffix": affected_suffix,
                    "recompute_targets": recompute_targets,
                    "reuse_targets": reuse_targets,
                    "suffix_rule": "recompute from min(affected) through end; reuse only strict prefix",
                }
                try:
                    self._epoch += 1
                    epoch = self._epoch
                    self.params = params
                    self.plan = plan
                    self.stats = new_stats
                    if params_changed:
                        self.params_version += 1
                    if plan_changed:
                        self.plan_version += 1
                    self.ready_version += 1
                    self._archive_ready()
                    self.ready = {}
                    self.ready_version += 1
                    self.stage = "invalidate"
                    self.last_invalidation = invalidation
                    self._persist_snapshot()
                except Exception:
                    self._epoch = prior["epoch"]
                    self.params = prior["params"]
                    self.plan = prior["plan"]
                    self.stats = prior["stats"]
                    self.params_version = prior["params_version"]
                    self.plan_version = prior["plan_version"]
                    self.ready_version = prior["ready_version"]
                    self.ready = prior["ready"]
                    self.superseded = prior["superseded"]
                    self.stage = prior["stage"]
                    self.last_invalidation = prior["last_invalidation"]
                    raise
            reused_ids = []
            for index in reuse_targets:
                old_record = current_ready[index]
                obj = self.cache.get(old_record.cache_key)
                texture, cache_record = self._rebind_cached(index, obj, params, plan, self.stats)
                with self._state_lock:
                    self.ready[index] = ReadySlice(
                        block=index,
                        pixel_range=texture.block.pixel_range,
                        cache_key=texture.cache_key,
                        semantic_key=texture.semantic_key,
                        content_hash=cache_record["content_hash"],
                        channel_digest=texture.channel_digest,
                        seam_digest=texture.seam_digest,
                        upstream_hash=texture.upstream_hash,
                        normalization_digest=texture.normalization_digest,
                        params_revision=params.revision,
                        params_digest=params.digest(),
                        plan_version=self.plan_version,
                        plan_digest=plan.digest(),
                        seed=params.seed,
                        node_path=texture.node_path,
                        duration_ms=texture.duration_ms,
                        source="cache_rebind",
                        estimates=texture.estimates,
                    )
                    self.ready_version += 1
                    self._persist_snapshot()
                reused_ids.append(index)
            recomputed_ids = []
            if generate_suffix:
                with self._state_lock:
                    missing = [index for index in recompute_targets if index not in self.ready]
                requested_parallelism = max(1, parallelism)
                degraded: list[str] = []
                if requested_parallelism > self.limits.max_parallel_slices:
                    if not allow_degrade:
                        fail(
                            "E_RESOURCE_LIMIT",
                            "修订重算并行数超过上限",
                            category="parallel_slices",
                            requested=requested_parallelism,
                            limit=self.limits.max_parallel_slices,
                        )
                    degraded.append("revision_recompute_serial_degraded")
                    requested_parallelism = 1
                workers = min(requested_parallelism, max(1, len(missing)))

                def invoke(index: int) -> dict[str, Any]:
                    return self._ensure_block(
                        index,
                        epoch=epoch,
                        params=params,
                        plan=plan,
                        stats=self.stats,
                        budget_ms=None,
                    )

                if workers == 1:
                    outputs = [invoke(index) for index in missing]
                else:
                    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="matforge-revision") as pool:
                        outputs = [future.result() for future in as_completed([pool.submit(invoke, index) for index in missing])]
                recomputed_ids = sorted(item["slice"]["block"] for item in outputs)
                with self._state_lock:
                    self.degradation = degraded
            report = {
                "ok": True,
                "changed_paths": changed_paths,
                "plan_changed": plan_changed,
                "affected": affected,
                "affected_suffix": affected_suffix,
                "recomputed": recomputed_ids,
                "reused": sorted(reused_ids),
                "degraded": self.degradation,
                "versions": self.snapshot_versions(),
            }
            with self._state_lock:
                report["coverage"] = {
                    "all_blocks": list(range(self.plan.block_count)),
                    "disjoint": set(recompute_targets).isdisjoint(reuse_targets),
                    "covers_all": set(recompute_targets) | set(reuse_targets) == set(range(self.plan.block_count)),
                    "planned_recompute": list(recompute_targets),
                    "planned_reuse": list(reuse_targets),
                    "generation_complete": len(recomputed_ids) == len(recompute_targets),
                }
                self.stage = "plan_committed"
            return report
        except MatForgeError as error:
            self._record_error(error)
            raise
        finally:
            self._commit_lock.release()

    def apply_revision(self, params: MaterialParams, **kwargs: Any) -> dict[str, Any]:
        return self.commit_plan(params, self.plan, **kwargs)

    def stitch(self) -> tuple[np.ndarray, list[dict[str, Any]]]:
        with self._state_lock:
            plan = self.plan
            records = dict(self.ready)
        missing = [index for index in range(plan.block_count) if index not in records]
        if missing:
            fail("E_BLOCK_MISSING", "拼接前仍有切片未就绪", missing=missing)
        arrays: dict[int, np.ndarray] = {}
        manifest = []
        for index in sorted(records):
            record = records[index]
            obj = self._cache_object_for_ready(record)
            arrays[index] = obj.channels
            manifest.append(
                {
                    "block": index,
                    "cache_key": record.cache_key,
                    "content_hash": record.content_hash,
                    "channel_digest": record.channel_digest,
                    "pixel_range": record.pixel_range,
                }
            )
        self.stage = "stitch"
        return stitch_blocks(plan, arrays), manifest


    def adjudicate(self, stitched: np.ndarray, full: FullTexture) -> dict[str, Any]:
        plan = self.plan
        if stitched.shape != full.channels.shape:
            fail(
                "E_BLOCK_MISMATCH",
                "拼接纹理与整幅纹理形状不一致",
                stitched_shape=list(stitched.shape),
                full_shape=list(full.channels.shape),
                deviation_spectrum=[],
            )
        difference = stitched.astype(np.int16) - full.channels.astype(np.int16)
        changed = np.any(difference != 0, axis=2)
        mismatch_count = int(changed.sum())
        if mismatch_count:
            y, x = (int(value) for value in np.argwhere(changed)[0])
            block_index = (y // plan.tile_height) * plan.cols + (x // plan.tile_width)
            spectrum = []
            for channel in range(difference.shape[2]):
                absolute = np.abs(difference[..., channel])
                spectrum.append(
                    {
                        "channel": channel,
                        "max_abs": int(absolute.max()),
                        "mean_abs": float(absolute.mean()),
                        "nonzero": int(np.count_nonzero(absolute)),
                        "histogram": [int(count) for count in np.bincount(absolute.ravel(), minlength=256)[:256]],
                    }
                )
            fail(
                "E_BLOCK_MISMATCH",
                "逐块拼接与整幅一次性生成不一致",
                first_deviation_pixel={"x": x, "y": y, "channel": int(np.argwhere(difference[y, x] != 0)[0][0])},
                block=block_index,
                mismatch_pixels=mismatch_count,
                deviation_spectrum=spectrum,
                stitched_digest=digest_obj(stitched.tolist()),
                full_digest=full.digest,
            )
        return {
            "ok": True,
            "equal": True,
            "mismatch_pixels": 0,
            "stitched_digest": full.digest,
            "full_digest": full.digest,
            "comparison": "byte_for_byte_all_channels",
        }

    def verify(self) -> dict[str, Any]:
        self.stage = "verify"
        started = time.perf_counter()
        try:
            with self._state_lock:
                params, plan = self.params, self.plan
                versions = self.snapshot_versions()
                invalidation = dict(self.last_invalidation) if self.last_invalidation else None
            stitched, manifest = self.stitch()
            full = generate_full(params, plan)
            adjudication = self.adjudicate(stitched, full)
            self.stage = "verified"
            return {
                **adjudication,
                "full": full.summary(),
                "stitched_shape": list(stitched.shape),
                "manifest": manifest,
                "versions": versions,
                "invalidation": invalidation,
                "basis": {
                    "noise_source": "global_coordinates_with_seed_and_explicit_octave_phase",
                    "normalization_digest": self.stats.digest,
                    "normalization": self.stats.to_dict(),
                    "seam_weight_source": "same_global_grid_edge_distance_formula",
                    "plan_digest": plan.digest(),
                    "params_digest": params.digest(),
                },
                "duration_ms": (time.perf_counter() - started) * 1000.0,
            }
        except MatForgeError as error:
            self._record_error(error)
            raise

    def replay_slice(self, block_id: int) -> dict[str, Any]:
        record = self.assert_slice_fresh(block_id)
        obj = self.cache.get(record.cache_key)
        regenerated = generate_block(self.params, self.plan, block_id, self.stats)
        if (
            obj.metadata.get("node_path") != record.node_path
            or obj.metadata.get("cache_key") != record.cache_key
            or obj.metadata.get("params_revision") != record.params_revision
            or regenerated.channel_digest != record.channel_digest
            or regenerated.seam_digest != record.seam_digest
            or regenerated.cache_key != record.cache_key
        ):
            fail(
                "E_STALE_SLICE",
                "重放记录与缓存对象节点、版本或像素摘要不一致",
                material_id=self.params.material_id,
                block=block_id,
                stale_version=obj.metadata.get("params_revision"),
                current_version=self.params.revision,
                cache_key=record.cache_key,
                recorded_channel_digest=record.channel_digest,
                replayed_channel_digest=regenerated.channel_digest,
            )
        return {
            "ok": True,
            "block": block_id,
            "node_path": record.node_path,
            "cache_key": record.cache_key,
            "params_revision": record.params_revision,
            "channel_digest": record.channel_digest,
            "seam_digest": record.seam_digest,
            "content_hash": record.content_hash,
            "channel_bytes": int(obj.channels.nbytes),
            "seam_bytes": int(obj.seam.nbytes),
            "replay_mode": "regenerated_and_compared_digest",
            "replayed_channel_digest": regenerated.channel_digest,
            "replayed_cache_key": regenerated.cache_key,
        }

    def inspect_stale(self, block_id: int) -> dict[str, Any]:
        with self._state_lock:
            record = self.superseded.get(block_id)
        if record is None:
            fail("E_BLOCK_NOT_FOUND", "没有可追溯的过期切片记录", block=block_id)
        error = self._freshness_error(record)
        if error is not None:
            raise error
        return {"ok": True, "fresh": True, "record": record.to_dict()}

    def selfcheck(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            verification = self.verify()
            checks.append({"name": "stitched_equals_full", "ok": True, "evidence": verification["comparison"]})
        except MatForgeError as error:
            self._record_error(error)
            checks.append({"name": "stitched_equals_full", "ok": False, "code": error.code, "details": error.details})
            verification = None
        with self._state_lock:
            records = dict(self.ready)
        key_checks = []
        for index, record in sorted(records.items()):
            try:
                cache_verification = self.cache.verify_entry(record.cache_key)
                obj = self.cache.get(record.cache_key)
                expected_semantic = semantic_cache_key(self.params, self.plan, index, self.stats.digest)
                consistent = (
                    cache_verification["channel_digest"] == record.channel_digest
                    and cache_verification["seam_digest"] == record.seam_digest
                    and obj.metadata.get("cache_key") == record.cache_key
                    and obj.metadata.get("node_path") == record.node_path
                    and expected_semantic == record.semantic_key
                )
                key_checks.append({"block": index, "ok": consistent})
            except MatForgeError as error:
                key_checks.append({"block": index, "ok": False, "code": error.code, "details": error.details})
        checks.append({"name": "cache_key_and_digest_crosscheck", "ok": all(item["ok"] for item in key_checks), "items": key_checks})
        invalidation = self.last_invalidation
        if invalidation:
            recomputed = set(invalidation.get("recompute_targets", []))
            reused = set(invalidation.get("reuse_targets", []))
            all_blocks = set(range(self.plan.block_count))
            checks.append(
                {
                    "name": "recompute_reuse_partition",
                    "ok": recomputed.isdisjoint(reused) and (recomputed | reused) == all_blocks,
                    "recomputed": sorted(recomputed),
                    "reused": sorted(reused),
                    "all_blocks": sorted(all_blocks),
                }
            )
        else:
            checks.append({"name": "recompute_reuse_partition", "ok": True, "note": "尚未发生参数修订"})
        try:
            snapshot_info = self.snapshot_store.inspect_file()
            checks.append({"name": "snapshot_checksums", "ok": True, "snapshot": snapshot_info})
        except MatForgeError as error:
            checks.append({"name": "snapshot_checksums", "ok": False, "code": error.code, "details": error.details})
        all_ok = all(check["ok"] for check in checks)
        return {
            "ok": all_ok,
            "checks": checks,
            "verification": verification,
            "versions": self.snapshot_versions(),
            "stage": self.stage,
            "last_error": self.last_error,
        }

    def snapshot_versions(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "params_version": self.params_version,
                "params_revision": self.params.revision,
                "plan_version": self.plan_version,
                "ready_version": self.ready_version,
                "counts": {"ready": len(self.ready), "total": self.plan.block_count},
                "checksums": {
                    "params": digest_obj(self.params.to_dict()),
                    "plan": self.plan.digest(),
                    "ready": digest_obj([record.to_dict() for _, record in sorted(self.ready.items())]),
                },
            }

    def load_snapshot(self) -> Snapshot:
        snapshot = self.snapshot_store.load()
        params_value = dict(snapshot.params["value"])
        noise_value = params_value.get("noise", {})
        if isinstance(noise_value, dict):
            params_value["noise"] = NoiseParams(**noise_value)
        params_value["base_color"] = tuple(params_value["base_color"])
        params = MaterialParams(**params_value)
        plan_value = snapshot.plan["value"]
        plan = BlockPlan(
            width=plan_value["width"],
            height=plan_value["height"],
            tile_width=plan_value["tile_width"],
            tile_height=plan_value["tile_height"],
        )
        params.validate()
        self._validate_resources(plan)
        records = {}
        for item in snapshot.ready["items"]:
            record = ReadySlice.from_dict(item)
            if record.block >= plan.block_count:
                fail("E_SNAPSHOT_CORRUPT", "快照切片编号超出计划范围", block=record.block)
            self.cache.get(record.cache_key)
            records[record.block] = record
        with self._state_lock:
            self.params = params
            self.plan = plan
            self.stats = compute_normalization(params, plan)
            self.params_version = snapshot.params["version"]
            self.plan_version = snapshot.plan["version"]
            self.ready_version = snapshot.ready["version"]
            self.ready = records
            self._epoch += 1
            self.stage = "snapshot_loaded"
        return snapshot

    def state(self) -> dict[str, Any]:
        with self._state_lock:
            blocks = []
            for block in self.plan.blocks():
                record = self.ready.get(block.index)
                stale = self.superseded.get(block.index)
                blocks.append(
                    {
                        **block.to_dict(),
                        "ready": record is not None,
                        "source": record.source if record else None,
                        "channel_digest": record.channel_digest if record else None,
                        "duration_ms": record.duration_ms if record else None,
                        "stale_record_version": stale.params_revision if stale else None,
                    }
                )
            return {
                "app": "MatForge",
                "stage": self.stage,
                "params": self.params.to_dict(),
                "params_digest": self.params.digest(),
                "plan": self.plan.to_dict(),
                "plan_digest": self.plan.digest(),
                "blocks": blocks,
                "progress": self.progress(),
                "last_observation": self.last_observation,
                "last_error": self.last_error,
                "last_invalidation": self.last_invalidation,
                "snapshot": self.snapshot_versions(),
                "normalization": self.stats.to_dict(),
                "degradation": list(self.degradation),
                "limits": asdict(self.limits),
                "cache": {
                    "root": str(self.cache.root),
                    "objects": len(self.cache.index_snapshot()),
                    "disk_bytes": self.cache.disk_bytes(),
                },
            }

    def capture_rgba(self) -> tuple[np.ndarray, dict[str, Any]]:
        """Render currently ready blocks against a checkerboard for the local UI."""
        rgba = np.empty((self.plan.height, self.plan.width, 4), dtype=np.uint8)
        rgba[..., :3] = 30
        rgba[..., 3] = 255
        manifest = []
        with self._state_lock:
            plan = self.plan
            records = dict(self.ready)
        for index, record in sorted(records.items()):
            obj = self._cache_object_for_ready(record)
            block = plan.block(index)
            rgba[block.y0 : block.y1, block.x0 : block.x1, :3] = obj.channels[..., :3]
            manifest.append(
                {
                    "block": index,
                    "pixel_range": block.pixel_range,
                    "cache_key": record.cache_key,
                    "content_hash": record.content_hash,
                }
            )
        for block in plan.blocks():
            record = records.get(block.index)
            if record is None:
                checker = ((np.indices((block.height, block.width)).sum(axis=0) // 8) % 2).astype(np.uint8)
                fill = (42 + checker * 20).astype(np.uint8)
                rgba[block.y0 : block.y1, block.x0 : block.x1, 0] = fill
                rgba[block.y0 : block.y1, block.x0 : block.x1, 1] = fill
                rgba[block.y0 : block.y1, block.x0 : block.x1, 2] = fill
                rgba[block.y0 : block.y1, block.x0 : block.x1, 3] = 255
        return rgba, manifest

