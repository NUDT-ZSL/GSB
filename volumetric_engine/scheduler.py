"""Bounded deterministic tile scheduler."""

from __future__ import annotations

import concurrent.futures
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .interrupts import CancellationToken
from .memory import tile_buffer_bytes as estimate_tile_buffer_bytes
from .model import Scene
from .renderer import (
    ImageResult,
    TileGeometry,
    TileResult,
    assemble_image,
    render_tile,
    tile_geometries,
)


def _render_tile_worker(scene: Scene, tile: TileGeometry, collect_trace: bool) -> TileResult:
    result = render_tile(scene, tile, collect_trace=collect_trace)
    # Deterministic test hook used by the interrupt-boundary integration test.
    delay = float(os.environ.get("GSB_TEST_TILE_DELAY", "0") or "0")
    if delay > 0.0:
        time.sleep(delay)
    return result


def tile_buffer_bytes(scene: Scene, tile: TileGeometry) -> int:
    return estimate_tile_buffer_bytes(tile.width, tile.height, scene.render.samples_per_pixel)


@dataclass
class SchedulerStats:
    peak_inflight_bytes: int = 0
    submitted_tiles: int = 0
    completed_tiles: int = 0
    max_workers: int = 0
    memory_limit_bytes: int = 0


def schedule_tiles(
    scene: Scene,
    tiles: list[TileGeometry],
    *,
    collect_trace: bool,
    on_result: Callable[[TileResult], None] | None = None,
    token: CancellationToken | None = None,
) -> tuple[dict[int, TileResult], SchedulerStats, bool]:
    """Render tiles with bounded workers and an in-flight sampling budget.

    Tasks are submitted strictly by tile index.  If the next tile cannot fit,
    no later tile is allowed to jump ahead.  A cancellation request prevents
    new submissions but lets already-running tiles reach their boundary and
    persist normally.
    """

    stats = SchedulerStats(
        max_workers=min(scene.render.max_workers, max(1, len(tiles))),
        memory_limit_bytes=scene.render.inflight_memory_limit_bytes,
    )
    if not tiles:
        return {}, stats, False

    results: dict[int, TileResult] = {}
    pending = deque(tiles)
    running: dict[concurrent.futures.Future[TileResult], tuple[TileGeometry, int]] = {}
    inflight = 0
    cancelled = False

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=stats.max_workers)
    try:
        while pending or running:
            while pending and not cancelled and len(running) < stats.max_workers:
                if token is not None and token.requested:
                    cancelled = True
                    break
                tile = pending[0]
                budget = tile_buffer_bytes(scene, tile)
                if inflight + budget > scene.render.inflight_memory_limit_bytes:
                    break
                pending.popleft()
                future = executor.submit(_render_tile_worker, scene, tile, collect_trace)
                running[future] = (tile, budget)
                inflight += budget
                stats.submitted_tiles += 1
                stats.peak_inflight_bytes = max(stats.peak_inflight_bytes, inflight)

            if not running:
                if pending and not cancelled:
                    # Validation should make this unreachable; keep the failure
                    # explicit instead of silently lowering resolution.
                    raise RuntimeError("one tile exceeds the in-flight memory budget")
                break

            done, _ = concurrent.futures.wait(
                running.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for future in done:
                tile, budget = running.pop(future)
                inflight -= budget
                result = future.result()
                results[tile.index] = result
                stats.completed_tiles += 1
                if on_result is not None:
                    on_result(result)

            if token is not None and token.requested:
                cancelled = True
    finally:
        executor.shutdown(wait=True, cancel_futures=False)

    return results, stats, cancelled


def render_blocks_in_memory(
    scene: Scene,
    *,
    collect_trace: bool = False,
    token: CancellationToken | None = None,
) -> ImageResult:
    tiles = tile_geometries(scene)
    results, scheduler_stats, cancelled = schedule_tiles(
        scene,
        tiles,
        collect_trace=collect_trace,
        token=token,
    )
    if cancelled:
        raise RuntimeError("in-memory render interrupted")
    if len(results) != len(tiles):
        raise RuntimeError("not all tiles completed")
    return assemble_image(
        scene,
        [results[index] for index in sorted(results)],
        collect_trace=collect_trace,
        extra_stats={"scheduler": scheduler_stats.__dict__.copy()},
    )

