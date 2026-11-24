"""Joint round-time / in-flight-memory budget scheduler.

The admission sequence is produced by a pure deterministic planner that only
consumes tile pixel counts and round budgets.  The thread pool then follows
that plan while bounded by ``concurrency`` and the in-flight pixel limit, so
changing worker count cannot change rounds, admission order or pixels.
"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading
from typing import Callable

from .errors import GracefulInterrupt
from .models import RenderTask, TileRect
from .tiles import tile_index


@dataclass(frozen=True)
class Round:
    round_no: int
    tile_indices: tuple[int, ...]
    logical_cost: int


def plan_rounds(task: RenderTask, tiles: list[TileRect]) -> list[Round]:
    """Greedy row-major packing; a tile that overflows starts a new round."""
    rounds: list[Round] = []
    current: list[int] = []
    current_cost = 0
    for tile in tiles:
        cost = tile.pixel_count
        if current and current_cost + cost > task.round_budget:
            rounds.append(Round(len(rounds), tuple(current), current_cost))
            current = []
            current_cost = 0
        current.append(tile_index(task, tile))
        current_cost += cost
    if current:
        rounds.append(Round(len(rounds), tuple(current), current_cost))
    return rounds


class InFlightGate:
    """Bounded pool gate tracking live worker slots and pixel buffers."""

    def __init__(self, concurrency: int, pixel_limit: int):
        self._condition = threading.Condition()
        self._concurrency = concurrency
        self._active = 0
        self._pixel_limit = pixel_limit
        self._in_flight_pixels = 0

    def try_acquire(self, pixels: int) -> bool:
        """Reserve a worker slot plus pixel budget together, or fail cleanly."""
        with self._condition:
            if (
                self._active >= self._concurrency
                or self._in_flight_pixels + pixels > self._pixel_limit
            ):
                return False
            self._active += 1
            self._in_flight_pixels += pixels
            return True

    def release(self, pixels: int) -> None:
        with self._condition:
            self._active -= 1
            self._in_flight_pixels -= pixels
            self._condition.notify_all()

    @property
    def in_flight_pixels(self) -> int:
        with self._condition:
            return self._in_flight_pixels


TileWorker = Callable[[TileRect], object]
StopEvent = Callable[[], bool]


def execute_plan(
    task: RenderTask,
    tiles: list[TileRect],
    rounds: list[Round],
    worker: TileWorker,
    on_complete: Callable[[int, object], None],
    *,
    stop_event: StopEvent | None = None,
    gate: InFlightGate | None = None,
) -> list[list[int]]:
    """Execute planned rounds and persist completions in deterministic order.

    ``on_complete`` is invoked in tile-index order per round with the worker
    result.  The logical clock advances by each completed tile's pixel count,
    matching :func:`plan_rounds` independently of thread timing.
    """
    gate = gate or InFlightGate(task.concurrency, task.in_flight_pixel_limit)
    stop_event = stop_event or (lambda: False)
    processed: list[list[int]] = []
    tile_by_index = {tile_index(task, tile): tile for tile in tiles}

    for round_plan in rounds:
        if stop_event():
            raise GracefulInterrupt("stop requested before round admission")
        round_done: list[int] = []
        pending_admit = list(round_plan.tile_indices)
        in_flight: dict = {}
        with ThreadPoolExecutor(max_workers=task.concurrency) as pool:
            # Phase 1: admit every tile planned for this round exactly once.
            # Worker slots and the live pixel budget throttle admission; a
            # saturated budget drains an already admitted tile first, which
            # avoids self-deadlock and keeps the admission sequence fixed.
            while pending_admit:
                if stop_event() and not in_flight:
                    pending_admit = []
                    break
                index = pending_admit[0]
                tile = tile_by_index[index]
                if not gate.try_acquire(tile.pixel_count):
                    # Validation guarantees one tile always fits, so a failed
                    # acquisition means older tiles hold the budget; drain one
                    # before retrying the same head-of-queue tile.
                    assert in_flight, "pixel budget must hold at least one tile"
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in done:
                        done_index, pixels = in_flight.pop(future)
                        result = future.result()
                        gate.release(pixels)
                        on_complete(done_index, result)
                        round_done.append(done_index)
                    continue
                if stop_event():
                    gate.release(tile.pixel_count)
                    pending_admit = []
                    break
                future = pool.submit(worker, tile)
                in_flight[future] = (index, tile.pixel_count)
                pending_admit.pop(0)

            # Phase 2: drain the remaining admitted tiles.  A stop event only
            # takes effect at tile boundaries, so admitted work always
            # completes and is persisted before the interrupt is reported.
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    index, pixels = in_flight.pop(future)
                    result = future.result()
                    gate.release(pixels)
                    on_complete(index, result)
                    round_done.append(index)

        round_done.sort()
        processed.append(round_done)
        if stop_event():
            raise GracefulInterrupt("stop requested at tile boundary")
    return processed
