"""生成编排：并发调度、内存预算、中断语义与恢复。"""

from __future__ import annotations

import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .channels import build_channels
from .errors import BusinessError, ErrorCode
from .exporting import export_material
from .model import GenConfig, MaterialSpec
from .persistence import (
    build_version_id,
    cleanup_tmp_files,
    clear_interrupt_marker,
    list_completed_indices,
    load_valid_chunk,
    reset_material_state,
    write_chunk,
    write_interrupt_marker,
)
from .tiling import ChunkPlan, chunk_index_at, compare_arrays, plan_chunks, render_tile, stitch

EXIT_GRACEFUL_STOP = 10


class StopController:
    """可处理终止信号：信号事件 + 轮询标记文件（Windows 友好）。"""

    def __init__(self, stop_flag_path: str | Path | None = None):
        self._event = threading.Event()
        self.stop_flag_path = Path(stop_flag_path) if stop_flag_path else None
        self._installed = False
        self._previous = {}

    def request_stop(self) -> None:
        self._event.set()

    def stop_requested(self) -> bool:
        if self._event.is_set():
            return True
        if self.stop_flag_path and self.stop_flag_path.exists():
            self._event.set()
            return True
        return False

    def install_signal_handlers(self) -> None:
        if self._installed:
            return
        for signame in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                self._previous[sig] = signal.signal(sig, self._handler)
            except (ValueError, OSError):
                # 非主线程或平台不支持时忽略，标记文件仍可触发停止。
                pass
        self._installed = True

    def _handler(self, signum, frame) -> None:  # noqa: ANN001
        self._event.set()


class MemoryBudget:
    """在途分块像素缓冲预算，按申请票据保证材质/分块索引序等待。"""

    def __init__(self, budget_bytes: int):
        self.budget = int(budget_bytes)
        self.used = 0
        self._cond = threading.Condition()
        self._ticket = 0
        self._served = 0

    def acquire(self, need: int) -> int:
        with self._cond:
            ticket = self._ticket
            self._ticket += 1
            # 单块本身超过总预算时无法再细分；若当前没有其它在途块则放行，
            # 避免永久死锁。存在其它在途块时仍等待，维持有界并发与 FIFO 顺序。
            while ticket != self._served or (
                self.used + need > self.budget and not (self.used == 0 and need >= self.budget)
            ):
                self._cond.wait()
            self.used += need
            self._served += 1
            self._cond.notify_all()
            return ticket

    def release(self, need: int) -> None:
        with self._cond:
            self.used -= need
            self._cond.notify_all()


def estimate_tile_bytes(plan: ChunkPlan) -> int:
    # 权重1 + height1 + roughness1 + color3 + normal3 = 9 个 float64 面。
    return plan.tile_w * plan.tile_h * 9 * np.dtype(np.float64).itemsize


@dataclass
class GenerationResult:
    material_id: str
    version_id: str
    status: str
    exit_code: int = 0
    recovered_indices: list[int] = field(default_factory=list)
    computed_indices: list[int] = field(default_factory=list)
    tile_count: int = 0
    consistency: dict = field(default_factory=dict)
    errors: list[BusinessError] = field(default_factory=list)
    export_info: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {
            "material_id": self.material_id,
            "version_id": self.version_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "recovered_indices": self.recovered_indices,
            "computed_indices": self.computed_indices,
            "tile_count": self.tile_count,
            "consistency": self.consistency,
            "errors": [e.to_dict() for e in self.errors],
            "export_info": self.export_info,
        }
        return data


class GenerationEngine:
    def __init__(
        self,
        config: GenConfig,
        stop_controller: StopController | None = None,
        force_crash_after: int | None = None,
    ):
        self.config = config
        self.stop = stop_controller or StopController()
        # 仅用于演示强制终止：在持久化指定数量分块后以 os._exit 硬退出。
        self.force_crash_after = force_crash_after
        self._persisted_count = 0

    def run_material(self, spec: MaterialSpec, state_root: str | Path, output_root: str | Path, archive_root: str | Path) -> GenerationResult:
        version_id = build_version_id(spec, self.config)
        plans = plan_chunks(spec)
        result = GenerationResult(material_id=spec.material_id, version_id=version_id, status="running")

        cleanup_tmp_files(state_root)
        valid, corrupt = list_completed_indices(state_root, spec.material_id, version_id)

        if corrupt:
            # 存在版本标识不一致或摘要损坏块：不混拼，整体重做。
            reset_material_state(state_root, spec.material_id)
            valid = []
            for index, code in sorted(corrupt.items()):
                result.errors.append(BusinessError(spec.material_id, code, f"已持久化分块 {index} 摘要无效，触发整体重做"))

        tiles_by_index: dict[int, dict] = {}
        for index in valid:
            tile, code = load_valid_chunk(state_root, spec.material_id, index, version_id)
            if tile is not None:
                tiles_by_index[index] = tile
                result.recovered_indices.append(index)
            else:
                reset_material_state(state_root, spec.material_id)
                tiles_by_index.clear()
                result.recovered_indices.clear()
                result.errors.append(BusinessError(spec.material_id, code or ErrorCode.E_CHUNK_DIGEST_MISMATCH, f"分块 {index} 恢复失败"))
                break

        pending = [plan for plan in plans if plan.index not in tiles_by_index]
        budget = MemoryBudget(self.config.memory_budget_bytes)

        def worker(plan: ChunkPlan) -> dict:
            need = estimate_tile_bytes(plan)
            budget.acquire(need)
            try:
                tile = render_tile(spec, plan)
                write_chunk(state_root, spec.material_id, version_id, plan, tile)
                return tile
            finally:
                budget.release(need)

        if pending:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as pool:
                in_flight = {}
                cursor = 0
                stop_seen = False
                while cursor < len(pending) or in_flight:
                    # 停止信号只阻止提交“下一个”块；已在途块跑完并持久化，
                    # 这样停止点严格落在分块边界，且不会留下半成品。
                    should_submit = (not stop_seen) and (not self.stop.stop_requested() or cursor == 0)
                    if should_submit:
                        while cursor < len(pending) and len(in_flight) < self.config.concurrency:
                            plan = pending[cursor]
                            future = pool.submit(worker, plan)
                            in_flight[future] = plan
                            cursor += 1
                            if self.stop.stop_requested():
                                # 停止已置位时，首批只保证提交一个在途块。
                                break
                    else:
                        stop_seen = True
                    if not in_flight:
                        break
                    future = next(iter(in_flight))
                    future.result()
                    finished = [f for f in in_flight if f.done()]
                    future = finished[0]
                    tile = future.result()
                    plan = in_flight.pop(future)
                    tiles_by_index[plan.index] = tile
                    result.computed_indices.append(plan.index)
                    self._persisted_count += 1
                    if self.force_crash_after is not None and self._persisted_count >= self.force_crash_after:
                        # 强制终止：不写报告、不留中断标记，模拟 kill -9 / 进程崩溃。
                        os._exit(137)
                    if stop_seen or self.stop.stop_requested():
                        stop_seen = True
                        # 已提交块自然跑完并持久化，随后只在分块边界停止，不提交新块。
                        remaining = [p.index for p in pending[cursor:]]
                        write_interrupt_marker(state_root, spec.material_id, remaining[0] if remaining else len(plans), version_id)
                        result.status = "interrupted"
                        result.exit_code = EXIT_GRACEFUL_STOP
                        result.errors.append(BusinessError(spec.material_id, ErrorCode.E_INTERRUPTED, "收到可处理终止信号，已在分块边界停止"))
                        result.tile_count = len(tiles_by_index)
                        return result

        if len(tiles_by_index) != len(plans):
            result.status = "incomplete"
            result.tile_count = len(tiles_by_index)
            return result

        clear_interrupt_marker(state_root, spec.material_id)
        tiles = [tiles_by_index[index] for index in sorted(tiles_by_index)]
        result.tile_count = len(tiles)

        # 整幅参考结果与分块拼接在同一全局坐标口径下裁决。
        reference = build_channels(spec)
        composite = stitch(tiles, spec, dtype=np.float64)
        consistency = self._check_consistency(spec, reference, composite)
        result.consistency = consistency
        if not consistency["passed"]:
            result.status = "consistency_failed"
            result.errors.append(
                BusinessError(
                    spec.material_id,
                    ErrorCode.E_UNKNOWN,
                    f"首个不一致像素 {consistency['first_mismatch']} 位于分块 {consistency['chunk_index']}",
                )
            )
            return result

        out_dtype = np.dtype(self.config.dtype)
        summary = self._build_summary(spec, version_id, result, consistency)
        result.export_info = export_material(
            output_root,
            archive_root,
            spec.material_id,
            version_id,
            composite,
            tiles,
            summary,
            dtype=out_dtype,
        )
        result.status = "completed"
        return result

    def _check_consistency(self, spec: MaterialSpec, reference: dict, composite: dict) -> dict:
        report = {"passed": True, "tolerance": spec.tolerance, "channels": {}}
        for name in composite:
            passed, mismatch, max_error = compare_arrays(reference[name], composite[name], spec.tolerance)
            entry = {"max_error": max_error, "passed": passed}
            if not passed and mismatch is not None:
                entry["first_mismatch"] = list(mismatch)
                entry["chunk_index"] = chunk_index_at(spec, mismatch[0], mismatch[1])
                report["passed"] = False
            report["channels"][name] = entry
        if not report["passed"]:
            first = next(
                (c for c in report["channels"].values() if not c["passed"]),
                None,
            )
            report["first_mismatch"] = first["first_mismatch"] if first else None
            report["chunk_index"] = first["chunk_index"] if first else None
        return report

    def _build_summary(self, spec: MaterialSpec, version_id: str, result: GenerationResult, consistency: dict) -> dict:
        return {
            "material_id": spec.material_id,
            "version_id": version_id,
            "status": "completed",
            "grid": {"rows": spec.grid_rows, "cols": spec.grid_cols},
            "tile_count": result.tile_count,
            "recovered_indices": result.recovered_indices,
            "computed_indices": result.computed_indices,
            "consistency": consistency,
            "chunk_summaries": self._chunk_summaries(spec),
        }

    def _chunk_summaries(self, spec: MaterialSpec) -> list[dict]:
        return [
            {
                "index": plan.index,
                "row": plan.row,
                "col": plan.col,
                "core": [plan.core_x, plan.core_y, plan.core_w, plan.core_h],
                "window": [plan.x0, plan.y0, plan.tile_w, plan.tile_h],
            }
            for plan in plan_chunks(spec)
        ]
