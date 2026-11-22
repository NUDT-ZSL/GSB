"""生成服务编排: 分块生成、已就绪块查询、一致性校验、并发与资源控制.

并发口径:
- 多个块预览请求可并发执行 (读已就绪块, 加锁保护);
- 同一时刻只允许一个块划分计划提交生效, 后到者立即 E_STAGE_BUSY;
- 并发生成任务数受信号量限制, 超限 E_RESOURCE_LIMIT.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .adjudicate import adjudicate, override_plan
from .anchor import deviation_spectrum, load_anchor, validate_anchor_strength
from .blocks import block_ranges, center_first_order, parse_grid
from .config import GenerationConfig
from .errors import (
    E_BLOCK_MISMATCH,
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    GenError,
)
from .pipeline import (
    Region,
    aggregate,
    derive_fusion_params,
    initial_field,
    iterate_block,
    iterate_global,
    seed_field,
)
from .snapshot import (
    KIND_BLOCK_PLAN,
    KIND_FUSION_PARAMS,
    KIND_READY_MANIFEST,
    SnapshotStore,
)
from .textcond import condition_vector

STAGE_IDLE = "IDLE"
STAGE_ADJUDICATE = "ADJUDICATE"
STAGE_SEED = "SEED_PIXELS"
STAGE_TEXT_COND = "TEXT_CONDITION"
STAGE_FUSION = "CONDITION_FUSION"
STAGE_ITERATE = "ITERATIVE_UPDATE"
STAGE_AGGREGATE = "AGGREGATE"
STAGE_VERIFY = "CONSISTENCY_VERIFY"
STAGE_DONE = "DONE"
STAGE_ERROR = "ERROR"


class RunResult:
    def __init__(self) -> None:
        self.plan: Dict[str, object] = {}
        self.first_block_ms: float = 0.0
        self.total_ms: float = 0.0
        self.image: bytes = b""
        self.consistency: Dict[str, object] = {}
        self.anchor_report: Optional[Dict[str, object]] = None
        self.versions: Dict[str, int] = {}


class GenerationService:
    """本地确定性条件生成服务."""

    def __init__(
        self,
        cfg: GenerationConfig,
        store: SnapshotStore,
        tokens: Sequence[str],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.tokens = list(tokens)
        self._commit_lock = threading.Lock()
        self._task_sem = threading.BoundedSemaphore(cfg.limits.max_concurrent_tasks)
        self._ready_lock = threading.Lock()
        self._ready: Dict[int, Tuple[Region, bytes]] = {}
        self.stage = STAGE_IDLE
        self.plan: Optional[Dict[str, object]] = None
        self.last_error: Optional[Dict[str, object]] = None
        self.last_result: Optional[RunResult] = None

    # ---- 状态查询 ----

    def ready_blocks(self) -> Dict[str, object]:
        """已就绪块清单与像素区间."""
        with self._ready_lock:
            entries = [
                {"block_id": bid, "region": list(region)}
                for bid, (region, _) in sorted(self._ready.items())
            ]
        return {
            "version": self.store.current_version(KIND_READY_MANIFEST),
            "ready": entries,
        }

    def preview_block(self, block_id: int) -> bytes:
        """并发安全的块预览查询."""
        with self._ready_lock:
            entry = self._ready.get(block_id)
        if entry is None:
            raise KeyError(f"block {block_id} not ready")
        return entry[1]

    # ---- 生成主流程 ----

    def run(
        self,
        grid: Optional[str] = None,
        anchor_ref: Optional[Tuple[bytes, Tuple[int, int, int]]] = None,
        anchor_strength: float = 0.0,
    ) -> RunResult:
        """执行一次完整生成; 全程持有计划提交锁, 冲突者立即 E_STAGE_BUSY."""
        if not self._task_sem.acquire(blocking=False):
            raise GenError(
                E_RESOURCE_LIMIT,
                "concurrent generation task limit reached",
                max_concurrent_tasks=self.cfg.limits.max_concurrent_tasks,
            )
        try:
            if not self._commit_lock.acquire(blocking=False):
                raise GenError(
                    E_STAGE_BUSY,
                    "another block plan commit is in effect",
                )
            try:
                return self._run_locked(grid, anchor_ref, anchor_strength)
            finally:
                self._commit_lock.release()
        finally:
            self._task_sem.release()

    def _run_locked(
        self,
        grid: Optional[str],
        anchor_ref: Optional[Tuple[bytes, Tuple[int, int, int]]],
        anchor_strength: float,
    ) -> RunResult:
        cfg = self.cfg
        result = RunResult()
        try:
            self.stage = STAGE_ADJUDICATE
            plan = adjudicate(cfg) if grid is None else override_plan(cfg, grid)
            self.plan = plan
            result.plan = plan

            self.stage = STAGE_TEXT_COND
            cond_vec = condition_vector(self.tokens, cfg.cond_dim, cfg.dtype)

            anchor_field: Optional[np.ndarray] = None
            if anchor_ref is not None:
                validate_anchor_strength(anchor_strength)
                anchor_field = load_anchor(cfg, anchor_ref[0], anchor_ref[1])

            self.stage = STAGE_FUSION
            fusion = derive_fusion_params(cond_vec, cfg)
            fusion_version = self.store.current_version(KIND_FUSION_PARAMS) + 1
            self.store.submit(KIND_FUSION_PARAMS, fusion_version, fusion.to_dict())
            plan_version = self.store.current_version(KIND_BLOCK_PLAN) + 1
            self.store.submit(KIND_BLOCK_PLAN, plan_version, plan)

            self.stage = STAGE_SEED
            noise = seed_field(cfg)

            rows, cols = parse_grid(str(plan["grid"]))
            ranges = block_ranges(cfg, rows, cols)
            order = center_first_order(rows, cols)

            self.stage = STAGE_ITERATE
            with self._ready_lock:
                self._ready.clear()
            fields: Dict[int, np.ndarray] = {}
            block_fields: List[Tuple[int, Region, np.ndarray]] = []
            start = time.perf_counter()
            first_ms: Optional[float] = None
            done: List[int] = []
            for bid in order:
                region = ranges[bid]
                field = iterate_block(
                    cfg,
                    fusion,
                    region,
                    noise=noise,
                    anchor=anchor_field,
                    anchor_strength=anchor_strength,
                )
                fields[bid] = field
                block_fields.append((bid, region, field))
                block_bytes = aggregate(field)
                with self._ready_lock:
                    self._ready[bid] = (region, block_bytes)
                done.append(bid)
                manifest_version = self.store.current_version(KIND_READY_MANIFEST) + 1
                self.store.submit(
                    KIND_READY_MANIFEST,
                    manifest_version,
                    {
                        "grid": plan["grid"],
                        "ready": [
                            {"block_id": b, "region": list(ranges[b])}
                            for b in sorted(done)
                        ],
                    },
                )
                if first_ms is None:
                    first_ms = (time.perf_counter() - start) * 1000.0
                    result.first_block_ms = first_ms
                    if first_ms > cfg.latency_budget_ms:
                        pending = [b for b in order if b not in done]
                        raise GenError(
                            E_LATENCY_BUDGET,
                            "first block latency exceeds budget",
                            observed_ms=round(first_ms, 3),
                            budget_ms=cfg.latency_budget_ms,
                            done_blocks=done,
                            pending_blocks=pending,
                        )

            self.stage = STAGE_AGGREGATE
            assembled = np.empty(
                (cfg.height, cfg.width, cfg.channels), dtype=cfg.dtype
            )
            for bid, region in enumerate(ranges):
                y0, y1, x0, x1 = region
                assembled[y0:y1, x0:x1, :] = fields[bid]
            image = aggregate(assembled)
            result.image = image
            result.total_ms = (time.perf_counter() - start) * 1000.0

            self.stage = STAGE_VERIFY
            result.consistency = self._verify(
                cfg, cond_vec, anchor_field, anchor_strength, image,
                ranges, int(plan["verify_tier"]),
            )

            if anchor_field is not None:
                result.anchor_report = deviation_spectrum(
                    cfg, block_fields, anchor_field
                )

            result.versions = {
                KIND_FUSION_PARAMS: fusion_version,
                KIND_BLOCK_PLAN: plan_version,
                KIND_READY_MANIFEST: self.store.current_version(KIND_READY_MANIFEST),
            }
            self.stage = STAGE_DONE
            self.last_error = None
            self.last_result = result
            return result
        except GenError as exc:
            self.stage = STAGE_ERROR
            self.last_error = exc.to_dict()
            raise

    # ---- 一致性校验 ----

    def _verify(
        self,
        cfg: GenerationConfig,
        cond_vec: np.ndarray,
        anchor_field: Optional[np.ndarray],
        anchor_strength: float,
        block_image: bytes,
        ranges: List[Region],
        tier: int,
    ) -> Dict[str, object]:
        fusion = derive_fusion_params(cond_vec, cfg)
        field0 = initial_field(cfg, fusion)
        global_final, _ = iterate_global(
            cfg, fusion, field0, anchor=anchor_field,
            anchor_strength=anchor_strength,
        )
        global_image = aggregate(global_final)
        if tier >= 2:
            if block_image != global_image:
                block_id, pixel = self._first_mismatch(
                    cfg, block_image, global_image, ranges
                )
                raise GenError(
                    E_BLOCK_MISMATCH,
                    "block-assembled image differs from global image",
                    block_id=block_id,
                    pixel=pixel,
                )
            return {"tier": tier, "verdict": "identical", "mode": "full-byte"}
        mism = self._boundary_mismatch(cfg, block_image, global_image, ranges)
        if mism is not None:
            block_id, pixel = mism
            raise GenError(
                E_BLOCK_MISMATCH,
                "boundary sample mismatch",
                block_id=block_id,
                pixel=pixel,
            )
        return {"tier": tier, "verdict": "identical", "mode": "boundary-sample"}

    @staticmethod
    def _first_mismatch(
        cfg: GenerationConfig,
        block_image: bytes,
        global_image: bytes,
        ranges: List[Region],
    ) -> Tuple[int, List[int]]:
        a = np.frombuffer(block_image, dtype=np.uint8).reshape(
            cfg.height, cfg.width, cfg.channels
        )
        b = np.frombuffer(global_image, dtype=np.uint8).reshape(
            cfg.height, cfg.width, cfg.channels
        )
        diff = np.argwhere(np.any(a != b, axis=2))
        y, x = int(diff[0][0]), int(diff[0][1])
        for bid, (y0, y1, x0, x1) in enumerate(ranges):
            if y0 <= y < y1 and x0 <= x < x1:
                return bid, [y, x]
        return -1, [y, x]

    @staticmethod
    def _boundary_mismatch(
        cfg: GenerationConfig,
        block_image: bytes,
        global_image: bytes,
        ranges: List[Region],
    ) -> Optional[Tuple[int, List[int]]]:
        a = np.frombuffer(block_image, dtype=np.uint8).reshape(
            cfg.height, cfg.width, cfg.channels
        )
        b = np.frombuffer(global_image, dtype=np.uint8).reshape(
            cfg.height, cfg.width, cfg.channels
        )
        for bid, (y0, y1, x0, x1) in enumerate(ranges):
            edges = []
            if y1 < cfg.height:
                edges.append((y1 - 1, slice(x0, x1)))
            if x1 < cfg.width:
                edges.append((slice(y0, y1), x1 - 1))
            for ys, xs in edges:
                if not np.array_equal(a[ys, xs, :], b[ys, xs, :]):
                    sub = np.argwhere(np.any(a[ys, xs, :] != b[ys, xs, :], axis=-1))
                    yy = ys if isinstance(ys, int) else y0 + int(sub[0][0])
                    xx = xs if isinstance(xs, int) else x0 + int(sub[0][0])
                    return bid, [int(yy), int(xx)]
        return None
