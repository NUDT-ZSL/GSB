from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .scene import BatchPlan, Scene


@dataclass(frozen=True)
class RequestPlan:
    request_id: str
    unit_key: str
    batch_id: str
    service: str
    tenant: str
    gradient_index: int
    concurrency: int
    wave_index: int
    request_index: int
    release_ms: int
    window_index: int


@dataclass(frozen=True)
class WavePlan:
    wave_index: int
    release_ms: int
    requests: tuple[RequestPlan, ...]


@dataclass(frozen=True)
class WindowPlan:
    window_key: str
    window_index: int
    start_ms: int
    end_ms: int
    waves: tuple[WavePlan, ...]
    requests: tuple[RequestPlan, ...]


@dataclass(frozen=True)
class UnitPlan:
    unit_key: str
    batch_id: str
    service: str
    tenant: str
    gradient_index: int
    concurrency: int
    start_ms: int
    end_ms: int
    cost_ms: int
    windows: tuple[WindowPlan, ...]

    @property
    def window_count(self) -> int:
        return len(self.windows)


@dataclass(frozen=True)
class Schedule:
    units: tuple[UnitPlan, ...]
    logical_start_ms: int
    logical_end_ms: int
    total_requests: int


def build_schedule(scene: Scene) -> Schedule:
    cursor = scene.clock.start_ms
    global_step_index = 0
    units: list[UnitPlan] = []
    total_requests = 0

    for batch in sorted(scene.batches, key=lambda item: item.batch_id):
        for gradient_index, concurrency in enumerate(batch.concurrency_gradients):
            if units:
                cursor += scene.clock.gradient_gap_ms
            gradient_start = cursor
            release_ms = gradient_start
            gradient_deadline = gradient_start + batch.duration_logical_ms
            wave_index = 0
            request_index = 0
            cost_ms = 0
            waves: list[WavePlan] = []
            unit_key = unit_key_for(batch.batch_id, batch.tenant, gradient_index, concurrency)
            while release_ms < gradient_deadline:
                window_index = (release_ms - gradient_start) // scene.clock.window_ms
                requests: list[RequestPlan] = []
                for sequence in range(concurrency):
                    request_id = f"{unit_key}|w{wave_index}|r{sequence}"
                    requests.append(
                        RequestPlan(
                            request_id=request_id,
                            unit_key=unit_key,
                            batch_id=batch.batch_id,
                            service=batch.service,
                            tenant=batch.tenant,
                            gradient_index=gradient_index,
                            concurrency=concurrency,
                            wave_index=wave_index,
                            request_index=request_index,
                            release_ms=release_ms,
                            window_index=window_index,
                        )
                    )
                    request_index += 1
                waves.append(WavePlan(wave_index=wave_index, release_ms=release_ms, requests=tuple(requests)))
                total_requests += len(requests)
                step = _next_step(scene, batch, global_step_index)
                global_step_index += 1
                cost_ms += step
                release_ms += step
                wave_index += 1

            windows: list[WindowPlan] = []
            grouped: dict[int, list[WavePlan]] = {}
            for wave in waves:
                grouped.setdefault(wave.requests[0].window_index, []).append(wave)
            for window_index in sorted(grouped):
                window_waves = grouped[window_index]
                window_start = gradient_start + window_index * scene.clock.window_ms
                window_end = window_start + scene.clock.window_ms
                window_requests = tuple(
                    request for wave in window_waves for request in wave.requests
                )
                windows.append(
                    WindowPlan(
                        window_key=window_key_for(unit_key, window_index),
                        window_index=window_index,
                        start_ms=window_start,
                        end_ms=window_end,
                        waves=tuple(window_waves),
                        requests=window_requests,
                    )
                )
            units.append(
                UnitPlan(
                    unit_key=unit_key,
                    batch_id=batch.batch_id,
                    service=batch.service,
                    tenant=batch.tenant,
                    gradient_index=gradient_index,
                    concurrency=concurrency,
                    start_ms=gradient_start,
                    end_ms=release_ms,
                    cost_ms=cost_ms,
                    windows=tuple(windows),
                )
            )
            cursor = release_ms

    return Schedule(
        units=tuple(units),
        logical_start_ms=scene.clock.start_ms,
        logical_end_ms=cursor,
        total_requests=total_requests,
    )


def unit_key_for(batch_id: str, tenant: str, gradient_index: int, concurrency: int) -> str:
    return f"{batch_id}|{tenant}|g{gradient_index}|c{concurrency}"


def window_key_for(unit_key: str, window_index: int) -> str:
    return f"{unit_key}|w{window_index}"


def _next_step(scene: Scene, batch: BatchPlan, global_step_index: int) -> int:
    if not scene.clock.step_table:
        return batch.request_pace_ms
    return scene.clock.step_table[global_step_index % len(scene.clock.step_table)]


def units_for_batch(schedule: Schedule, batch_id: str) -> Iterable[UnitPlan]:
    return (unit for unit in schedule.units if unit.batch_id == batch_id)

