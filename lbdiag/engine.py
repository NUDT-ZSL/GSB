"""Logical-clock-driven health-check execution and recovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    E_CLOCK_ROLLBACK,
    E_INTERNAL,
    E_PROBE_IO,
    BusinessError,
    fail,
)
from .filelock import InstanceFileLock
from .model import ScheduledCheck, WorkbenchConfig, schedule_checks
from .probe import probe_instance
from .storage import Store


@dataclass
class LogicalClock:
    """A deterministic millisecond clock with rollback detection."""

    now_ms: int
    step_ms: int
    rollback_tolerance_ms: int
    high_water_ms: int
    ticks: int = 0

    def check_rollback(self, requested_ms: int) -> None:
        self.high_water_ms = max(self.high_water_ms, requested_ms)
        if requested_ms < self.high_water_ms - self.rollback_tolerance_ms:
            fail(
                E_CLOCK_ROLLBACK,
                "logical clock moved backwards beyond the configured tolerance",
                fields={
                    "requested_ms": requested_ms,
                    "high_water_ms": self.high_water_ms,
                    "tolerance_ms": self.rollback_tolerance_ms,
                },
                exit_code=2,
            )

    def advance_to(self, target_ms: int) -> None:
        if target_ms < self.now_ms:
            return
        while self.now_ms < target_ms:
            self.now_ms = min(target_ms, self.now_ms + self.step_ms)
            self.ticks += 1
        self.high_water_ms = max(self.high_water_ms, self.now_ms)

    def advance_once(self) -> None:
        self.now_ms += self.step_ms
        self.ticks += 1
        self.high_water_ms = max(self.high_water_ms, self.now_ms)


def normalize_selection(
    config: WorkbenchConfig, values: Iterable[str] | None
) -> tuple[str, ...]:
    if not values:
        return tuple(instance.instance_id for instance in config.instances)
    selected: set[str] = set()
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                selected.add(item)
    unknown = sorted(selected - {instance.instance_id for instance in config.instances})
    if unknown:
        fail(
            "E_BAD_INSTANCE",
            "selected instances are not present in the sample",
            fields={"unknown_instance_ids": unknown},
        )
    return tuple(sorted(selected))


def _internal_failure_outcome(
    instance_id: str, scheduled: ScheduledCheck, exc: BaseException
) -> dict[str, Any]:
    return {
        "instance_id": instance_id,
        "check_index": scheduled.check_index,
        "scheduled_at_ms": scheduled.scheduled_at_ms,
        "jitter_offset_ms": scheduled.jitter_offset_ms,
        "classification": "malformed_response",
        "error_code": E_PROBE_IO,
        "http_status": None,
        "expected_status": None,
        "body_sha256": None,
        "expected_body_sha256": None,
        "response_bytes": 0,
        "abort_requested": True,
        "detail_code": E_INTERNAL,
        "measured_elapsed_us": 0,
        "internal_error": f"{type(exc).__name__}: {exc}",
    }


def _crash_hook(probe_count: int, limit: int | None) -> None:
    if limit is not None and probe_count >= limit:
        os._exit(137)


def run_workbench(
    store: Store,
    config: WorkbenchConfig,
    *,
    selected_instances: Iterable[str] | None = None,
    now_ms: int | None = None,
    lock_dir: str | Path | None = None,
    lock_timeout_ms: int = 30_000,
    crash_after_probes: int | None = None,
) -> dict[str, Any]:
    selected = normalize_selection(config, selected_instances)
    store.initialize(config)
    persisted_watermark = store.watermark()
    default_now = max(config.diagnostics.logical_clock_start_ms, persisted_watermark)
    requested_now = default_now if now_ms is None else now_ms
    high_water = max(persisted_watermark, config.diagnostics.logical_clock_start_ms - 1)
    clock = LogicalClock(
        now_ms=requested_now,
        step_ms=config.diagnostics.logical_clock_step_ms,
        rollback_tolerance_ms=config.diagnostics.clock_rollback_tolerance_ms,
        high_water_ms=high_water,
    )
    clock.check_rollback(requested_now)

    schedules = schedule_checks(config)
    locks_root = Path(lock_dir) if lock_dir is not None else store.path.parent / "locks"
    lock_root = locks_root / config.sample_sha256[:16]
    executed = 0
    reused = 0
    for instance_id in selected:
        instance = config.instance(instance_id)
        with InstanceFileLock(
            lock_root / f"{instance_id}.lock", timeout_ms=lock_timeout_ms
        ):
            store.commit_windows(instance_id, config)
            existing = store.get_check_indices(instance_id)
            for scheduled in schedules[instance_id]:
                if scheduled.check_index in existing:
                    reused += 1
                    continue
                clock.advance_to(scheduled.scheduled_at_ms)
                try:
                    outcome = probe_instance(instance, scheduled, config.plan.timeout_ms)
                except BaseException as exc:
                    outcome = _internal_failure_outcome(instance_id, scheduled, exc)
                inserted = store.insert_check(outcome)
                if inserted:
                    executed += 1
                    _crash_hook(executed, crash_after_probes)
                else:
                    reused += 1
                store.commit_windows(instance_id, config)
            store.commit_windows(instance_id, config)
    snapshot = store.snapshot(config)
    snapshot["execution"] = {
        "selected_instances": list(selected),
        "clock_ticks": clock.ticks,
        "logical_now_ms": clock.now_ms,
    }
    return snapshot

