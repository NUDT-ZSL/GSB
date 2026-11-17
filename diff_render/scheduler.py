"""Deterministic tiled rendering with a bounded thread pool and memory budget.

Tiles are submitted in a fixed row-major order.  Each worker computes an
independent per-tile loss and parameter gradient; the driver then accumulates
those partials in the canonical tile order (never in completion order), which
guarantees bit-identical results for any ``max_workers`` value, including the
serial path.

A counting semaphore bounds the number of in-flight tile buffers so that the
total sampled/gradient buffer allocation never exceeds the configured byte
budget.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np

from .errors import TileError
from .shading import backward_samples, forward_samples


@dataclass(frozen=True)
class Tile:
    index: int
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    sample_count: int


def build_tiles(config: Dict[str, Any], geometry: Dict[str, Any]) -> List[Tile]:
    width = geometry["width"]
    height = geometry["height"]
    tile_width = int(config["scheduler"]["tile_width"])
    tile_height = int(config["scheduler"]["tile_height"])
    samples = geometry["samples"]

    tiles: List[Tile] = []
    index = 0
    for row_start in range(0, height, tile_height):
        for col_start in range(0, width, tile_width):
            row_end = min(row_start + tile_height, height)
            col_end = min(col_start + tile_width, width)
            sample_count = (row_end - row_start) * (col_end - col_start) * samples
            tiles.append(
                Tile(index, row_start, row_end, col_start, col_end, sample_count)
            )
            index += 1
    return tiles


def tile_sample_mask(geometry: Dict[str, Any], tile: Tile) -> np.ndarray:
    width = geometry["width"]
    pixel_index = geometry["pixel_index"]
    row = pixel_index // width
    col = pixel_index % width
    return (
        (row >= tile.row_start)
        & (row < tile.row_end)
        & (col >= tile.col_start)
        & (col < tile.col_end)
    )


def estimate_tile_bytes(max_sample_count: int) -> int:
    """Conservative per-tile buffer estimate in bytes (float64 working set)."""
    # Color (3), normals (3), residual color gradient (3), a two-light cache of
    # ~14 scalar arrays, gradient pair (2), plus index/mask bookkeeping.
    arrays_per_sample = 3 + 3 + 3 + 14 * 2 + 1
    return int(max_sample_count * arrays_per_sample * 8)


class MemoryGate:
    """Tracks live tile-buffer allocations and enforces the byte budget."""

    def __init__(self, budget_bytes: int):
        self.budget_bytes = int(budget_bytes)
        self._live = 0
        self._peak = 0
        self._lock = threading.Lock()

    def acquire(self, size_bytes: int) -> None:
        with self._lock:
            if self._live + size_bytes > self.budget_bytes:
                raise TileError(
                    "tile buffer allocation would exceed memory budget",
                    {
                        "live_bytes": self._live,
                        "tile_bytes": size_bytes,
                        "budget_bytes": self.budget_bytes,
                    },
                )
            self._live += size_bytes
            self._peak = max(self._peak, self._live)

    def release(self, size_bytes: int) -> None:
        with self._lock:
            self._live -= size_bytes

    @property
    def peak_bytes(self) -> int:
        return self._peak


def _compute_tile(
    tile: Tile,
    config: Dict[str, Any],
    geometry: Dict[str, Any],
    params: np.ndarray,
    target: np.ndarray,
    memory_gate: MemoryGate,
) -> Tuple[int, float, np.ndarray]:
    """Evaluate loss and analytic gradient for one tile."""
    tile_bytes = estimate_tile_bytes(tile.sample_count)
    memory_gate.acquire(tile_bytes)
    try:
        mask = tile_sample_mask(geometry, tile)
        local_geometry = {
            **geometry,
            "normals": geometry["normals"][mask],
            "pixel_index": geometry["pixel_index"][mask],
            "sample_count": int(np.count_nonzero(mask)),
        }
        cache = forward_samples(params, local_geometry)

        # Tile-local pixel aggregation for the loss term.
        local_pixel_index = local_geometry["pixel_index"]
        unique_pixels = np.unique(local_pixel_index)
        local_image = np.zeros((unique_pixels.shape[0], 3), dtype=np.float64)
        remap = {int(pixel): slot for slot, pixel in enumerate(unique_pixels)}
        remapped_index = np.array([remap[int(p)] for p in local_pixel_index], dtype=np.int64)
        np.add.at(local_image, remapped_index, cache.color)
        local_image = local_image / geometry["samples"]

        local_target = target[unique_pixels]
        residual = local_image - local_target
        loss = float(np.sum(residual * residual))

        d_image = 2.0 * residual
        d_color = d_image[remapped_index] / geometry["samples"]
        gradient = backward_samples(cache, d_color, local_geometry)
        return tile.index, loss, gradient
    except TileError:
        raise
    except Exception as exc:  # noqa: BLE001 - propagate as first tile error
        raise TileError("tile %d failed: %s" % (tile.index, exc),
                        {"tile_index": tile.index})
    finally:
        memory_gate.release(tile_bytes)


def _accumulate_tiles(
    tiles: List[Tile],
    partials: List[Tuple[int, float, np.ndarray]],
) -> Tuple[float, np.ndarray]:
    """Fixed-order reduction: tile index order, identical float operations."""
    ordered = sorted(partials, key=lambda item: item[0])
    loss = 0.0
    gradient = np.zeros(2, dtype=np.float64)
    for _, tile_loss, tile_gradient in ordered:
        loss += tile_loss
        gradient = gradient + tile_gradient
    return loss, gradient


def evaluate_tiled(
    config: Dict[str, Any],
    geometry: Dict[str, Any],
    params: np.ndarray,
    target: np.ndarray,
    *,
    parallel: bool,
) -> Tuple[float, np.ndarray, Dict[str, Any]]:
    """Evaluate loss + gradient over all tiles serially or with a bounded pool."""
    tiles = build_tiles(config, geometry)
    budget = int(config["memory"]["buffer_byte_budget"])
    largest = max((tile.sample_count for tile in tiles), default=1)
    per_tile_bytes = estimate_tile_bytes(largest)
    if per_tile_bytes > budget:
        raise TileError(
            "memory budget is smaller than a single tile's working set",
            {"budget_bytes": budget, "min_required_bytes": per_tile_bytes},
        )
    max_in_flight = max(1, budget // per_tile_bytes)
    max_workers = int(config["scheduler"]["max_workers"])
    memory_gate = MemoryGate(budget)

    if not parallel or max_workers <= 1 or len(tiles) == 1:
        partials = []
        for tile in tiles:
            try:
                partials.append(
                    _compute_tile(tile, config, geometry, params, target, memory_gate)
                )
            except TileError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise TileError("tile %d failed: %s" % (tile.index, exc),
                                {"tile_index": tile.index})
        stats = {"mode": "serial", "workers": 1, "max_in_flight": 1,
                 "tile_count": len(tiles), "peak_buffer_bytes": memory_gate.peak_bytes}
    else:
        in_flight = threading.BoundedSemaphore(min(max_workers, max_in_flight))
        partials: List[Tuple[int, float, np.ndarray]] = []
        first_error: List[BaseException] = []

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = []

            def runner(tile: Tile):
                with in_flight:
                    if first_error:
                        raise TileError("cancelled after an earlier tile failure",
                                        {"tile_index": tile.index})
                    try:
                        return _compute_tile(
                            tile, config, geometry, params, target, memory_gate
                        )
                    except TileError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - normalize tile faults
                        raise TileError("tile %d failed: %s" % (tile.index, exc),
                                        {"tile_index": tile.index})

            for tile in tiles:
                futures.append(pool.submit(runner, tile))

            # Collect in submission order; the first failing future cancels all
            # remaining pending work.
            for future in futures:
                try:
                    partials.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    if not first_error:
                        first_error.append(exc)
                    for pending in futures:
                        pending.cancel()
            if first_error:
                raise first_error[0]

        stats = {
            "mode": "parallel",
            "workers": max_workers,
            "max_in_flight": min(max_workers, max_in_flight),
            "tile_count": len(tiles),
            "peak_buffer_bytes": memory_gate.peak_bytes,
        }

    loss, gradient = _accumulate_tiles(tiles, partials)
    return loss, gradient, stats
