"""Compute-budget admission and non-blocking calibration commit gate."""

from __future__ import annotations

import fcntl
import math
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from .errors import BudgetExceededError, StageBusyError


@dataclass(frozen=True)
class BudgetConfig:
    max_reconstruction_comparisons: int = 200_000
    max_resident_feature_bytes: int = 20_000_000
    feature_float_arrays_per_window: int = 3
    feature_scalar_count: int = 8
    bytes_per_float: int = 8
    python_record_overhead_bytes: int = 512


@dataclass
class BudgetDecision:
    admitted: bool
    degraded: bool
    strategy: str
    requested_windows: int
    admitted_windows: int
    reconstruction_comparisons: int
    resident_feature_bytes: int
    original_reconstruction_comparisons: int
    original_resident_feature_bytes: int
    limits: dict
    selected_window_ids: list[str] = field(default_factory=list)
    decision_basis: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "degraded": self.degraded,
            "strategy": self.strategy,
            "requested_windows": self.requested_windows,
            "admitted_windows": self.admitted_windows,
            "reconstruction_comparisons": self.reconstruction_comparisons,
            "resident_feature_bytes": self.resident_feature_bytes,
            "original_reconstruction_comparisons": self.original_reconstruction_comparisons,
            "original_resident_feature_bytes": self.original_resident_feature_bytes,
            "limits": self.limits,
            "selected_window_ids": list(self.selected_window_ids),
            "decision_basis": list(self.decision_basis),
        }


def estimate_batch(n_windows: int, input_dim: int, config: BudgetConfig | None = None) -> dict[str, int]:
    cfg = config or BudgetConfig()
    n_windows = max(0, int(n_windows))
    input_dim = max(1, int(input_dim))
    comparisons = n_windows * input_dim
    bytes_per_window = (
        cfg.feature_float_arrays_per_window * input_dim * cfg.bytes_per_float
        + cfg.feature_scalar_count * cfg.bytes_per_float
        + cfg.python_record_overhead_bytes
    )
    return {
        "n_windows": n_windows,
        "input_dim": input_dim,
        "reconstruction_comparisons": comparisons,
        "bytes_per_window": bytes_per_window,
        "resident_feature_bytes": n_windows * bytes_per_window,
    }


def admit_calibration_batch(
    window_ids: Sequence[str],
    input_dim: int,
    config: BudgetConfig | None = None,
    pinned_window_ids: Sequence[str] | None = None,
) -> BudgetDecision:
    cfg = config or BudgetConfig()
    ids = [str(item) for item in window_ids]
    pinned = {str(item) for item in (pinned_window_ids or [])}
    original = estimate_batch(len(ids), input_dim, cfg)
    limits = {
        "max_reconstruction_comparisons": cfg.max_reconstruction_comparisons,
        "max_resident_feature_bytes": cfg.max_resident_feature_bytes,
    }
    basis = [
        f"comparisons={original['reconstruction_comparisons']} = n_windows * input_dim",
        f"feature_memory={original['resident_feature_bytes']} = n_windows * {original['bytes_per_window']} bytes/window",
    ]

    within = (
        original["reconstruction_comparisons"] <= cfg.max_reconstruction_comparisons
        and original["resident_feature_bytes"] <= cfg.max_resident_feature_bytes
    )
    if within:
        return BudgetDecision(
            admitted=True,
            degraded=False,
            strategy="full_batch",
            requested_windows=len(ids),
            admitted_windows=len(ids),
            reconstruction_comparisons=original["reconstruction_comparisons"],
            resident_feature_bytes=original["resident_feature_bytes"],
            original_reconstruction_comparisons=original["reconstruction_comparisons"],
            original_resident_feature_bytes=original["resident_feature_bytes"],
            limits=limits,
            selected_window_ids=ids,
            decision_basis=basis + ["within_budget"],
        )

    max_by_comparisons = max(1, cfg.max_reconstruction_comparisons // max(1, input_dim))
    max_by_memory = max(1, cfg.max_resident_feature_bytes // max(1, original["bytes_per_window"]))
    admitted_count = min(len(ids), max_by_comparisons, max_by_memory)
    pinned_ids = [window_id for window_id in ids if window_id in pinned]
    if len(pinned_ids) > admitted_count:
        compact_bytes_per_window = 2 * cfg.bytes_per_float + 128
        compact_memory = len(pinned_ids) * compact_bytes_per_window
        if compact_memory > cfg.max_resident_feature_bytes:
            error = BudgetExceededError(
                "pinned calibration corrections exceed compact budget",
                details={
                    "pinned_windows": pinned_ids,
                    "compact_memory": compact_memory,
                    "limit": cfg.max_resident_feature_bytes,
                },
            )
            return BudgetDecision(
                admitted=False,
                degraded=True,
                strategy="reject",
                requested_windows=len(ids),
                admitted_windows=0,
                reconstruction_comparisons=original["reconstruction_comparisons"],
                resident_feature_bytes=original["resident_feature_bytes"],
                original_reconstruction_comparisons=original["reconstruction_comparisons"],
                original_resident_feature_bytes=original["resident_feature_bytes"],
                limits=limits,
                decision_basis=basis + [error.message],
            )
        selected = pinned_ids
        strategy = "pinned_scalar_sketch"
        admitted_count = len(selected)
        resident_bytes = compact_memory
        comparisons = admitted_count * input_dim
        basis.append("pinned_set_exceeds_stratified_cap; retain scalar score sketch for corrections")
    else:
        selected = _stratified_select(ids, admitted_count, pinned)
        strategy = "deterministic_stratified_subsample"
        reduced = estimate_batch(len(selected), input_dim, cfg)
        resident_bytes = reduced["resident_feature_bytes"]
        comparisons = reduced["reconstruction_comparisons"]
        basis.append(
            f"reduce_to={admitted_count}; caps comparison={max_by_comparisons}, memory={max_by_memory}"
        )

    return BudgetDecision(
        admitted=True,
        degraded=True,
        strategy=strategy,
        requested_windows=len(ids),
        admitted_windows=admitted_count,
        reconstruction_comparisons=comparisons,
        resident_feature_bytes=resident_bytes,
        original_reconstruction_comparisons=original["reconstruction_comparisons"],
        original_resident_feature_bytes=original["resident_feature_bytes"],
        limits=limits,
        selected_window_ids=selected,
        decision_basis=basis + ["degraded_path_recorded"],
    )


def _stratified_select(ids: Sequence[str], count: int, pinned: set[str]) -> list[str]:
    ids_list = [str(item) for item in ids]
    count = min(int(count), len(ids_list))
    selected: list[str] = [window_id for window_id in ids_list if window_id in pinned]
    remaining = [window_id for window_id in ids_list if window_id not in pinned]
    remaining_count = max(0, count - len(selected))
    if remaining_count <= 0:
        return selected[:count]
    if remaining_count >= len(remaining):
        return selected + remaining
    positions = np.linspace(0, len(remaining) - 1, remaining_count)
    chosen_indices = sorted({int(round(float(position))) for position in positions})
    # Rounding can collapse endpoints for tiny arrays; fill deterministically.
    index = 0
    while len(chosen_indices) < remaining_count and index < len(remaining):
        chosen_indices.append(index)
        chosen_indices = sorted(set(chosen_indices))
        index += 1
    selected.extend(remaining[index] for index in chosen_indices[:remaining_count])
    return selected


class CalibrationGate:
    """Non-blocking process-wide and cross-process calibration lock."""

    def __init__(self, state_dir: str | Path, lock_name: str = "calibration.lock"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.state_dir / lock_name
        self._thread_lock = threading.Lock()
        self._handle = None
        self.held = False

    def try_acquire(self) -> bool:
        if self.held:
            return False
        if not self._thread_lock.acquire(blocking=False):
            return False
        try:
            handle = open(self.lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                self._thread_lock.release()
                return False
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={threading.get_ident()}\n")
            handle.flush()
            self._handle = handle
            self.held = True
            return True
        except Exception:
            if self._thread_lock.locked():
                self._thread_lock.release()
            raise

    def acquire_or_busy(self) -> None:
        if not self.try_acquire():
            raise StageBusyError(
                "another threshold calibration commit is active",
                details={"lock": str(self.lock_path), "error_code": StageBusyError.code},
            )

    def release(self) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None
        if self.held:
            self.held = False
            try:
                self._thread_lock.release()
            except RuntimeError:
                pass

    def __enter__(self) -> "CalibrationGate":
        self.acquire_or_busy()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
