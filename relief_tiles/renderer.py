"""Preview-first, resumable, bounded-concurrency block rendering."""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import ReliefConfig
from .errors import BusinessError, RenderInterrupted
from .gray import (
    NormalizationStats,
    compute_normalization_stats,
    generate_full_gray,
    quantize_raw,
    raw_from_block,
)
from .store import (
    BlockRecord,
    BlockStore,
    InvalidBlock,
    LoadedBlock,
)


@dataclass
class BlockOutcome:
    row: int
    col: int
    status: str
    digest: str
    invalid_reason: str | None = None
    duration_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "col": self.col,
            "status": self.status,
            "digest": self.digest,
            "invalid_reason": self.invalid_reason,
            "duration_ms": self.duration_ms,
        }


@dataclass
class PreviewResult:
    row: int
    col: int
    gray: np.ndarray
    cached: bool
    record: BlockRecord
    invalid_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": self.record.version,
            "block": {
                "row": self.row,
                "col": self.col,
                "x0": self.record.x0,
                "y0": self.record.y0,
                "width": self.record.width,
                "height": self.record.height,
                "pixel_count": self.record.pixel_count,
                "digest": self.record.digest,
            },
            "cached": self.cached,
            "invalid_reason": self.invalid_reason,
        }


@dataclass
class RenderResult:
    mosaic: np.ndarray
    report: dict[str, Any]
    first_mismatch: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.first_mismatch is None


def _compute_block(
    config: ReliefConfig,
    stats: NormalizationStats,
    row: int,
    col: int,
    delay_ms: int,
) -> np.ndarray:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000.0)
    raw, _height_window = raw_from_block(config, row, col)
    return quantize_raw(raw, stats, config)


def _seam_metrics(mosaic: np.ndarray, config: ReliefConfig) -> dict[str, Any]:
    vertical_values: list[np.ndarray] = []
    horizontal_values: list[np.ndarray] = []
    for col in range(1, config.cols):
        x = col * config.block_width
        vertical_values.append(
            np.abs(mosaic[:, x - 1].astype(np.int32) - mosaic[:, x].astype(np.int32))
        )
    for row in range(1, config.rows):
        y = row * config.block_height
        horizontal_values.append(
            np.abs(mosaic[y - 1, :].astype(np.int32) - mosaic[y, :].astype(np.int32))
        )
    vertical_max = int(max((int(values.max()) for values in vertical_values), default=0))
    horizontal_max = int(
        max((int(values.max()) for values in horizontal_values), default=0)
    )
    return {
        "seam_max_abs_adjacent_delta": max(vertical_max, horizontal_max),
        "vertical_seam_max_abs_adjacent_delta": vertical_max,
        "horizontal_seam_max_abs_adjacent_delta": horizontal_max,
        "vertical_seam_count": len(vertical_values),
        "horizontal_seam_count": len(horizontal_values),
    }


def _first_mismatch(
    actual: np.ndarray,
    expected: np.ndarray,
    config: ReliefConfig,
) -> dict[str, Any] | None:
    differing = np.flatnonzero(
        np.asarray(actual != expected, dtype=np.bool_).reshape(-1)
    )
    if differing.size == 0:
        return None
    flat_index = int(differing[0])
    y, x = divmod(flat_index, config.width)
    row = y // config.block_height
    col = x // config.block_width
    difference = np.abs(actual.astype(np.int64) - expected.astype(np.int64))
    return {
        "x": x,
        "y": y,
        "block": {"row": row, "col": col},
        "channel": 0,
        "actual": int(actual[y, x]),
        "reference": int(expected[y, x]),
        "max_abs_diff": int(difference.max()),
        "different_pixel_count": int(differing.size),
    }


class ReliefRenderer:
    def __init__(self, config: ReliefConfig, state_dir: str) -> None:
        self.config = config
        self.store = BlockStore(state_dir, config)
        self.store.initialize()

    def _ensure_normalization(self) -> NormalizationStats:
        stats = self.store.load_normalization()
        if stats is not None:
            return stats
        stats = compute_normalization_stats(self.config)
        self.store.save_normalization(stats)
        return stats

    def _persist_computed_block(
        self, row: int, col: int, gray: np.ndarray
    ) -> BlockRecord:
        return self.store.save_block(row, col, gray)

    def preview_block(
        self, row: int, col: int, block_delay_ms: int = 0
    ) -> PreviewResult:
        self.config.block_bounds(row, col)
        stats = self._ensure_normalization()
        loaded = self.store.load_block(row, col)
        invalid_reason: str | None = None
        if isinstance(loaded, LoadedBlock):
            return PreviewResult(
                row=row,
                col=col,
                gray=loaded.gray,
                cached=True,
                record=loaded.record,
                invalid_reason=None,
            )
        if isinstance(loaded, InvalidBlock):
            invalid_reason = loaded.reason
        gray = _compute_block(self.config, stats, row, col, block_delay_ms)
        record = self._persist_computed_block(row, col, gray)
        return PreviewResult(
            row=row,
            col=col,
            gray=gray,
            cached=False,
            record=record,
            invalid_reason=invalid_reason,
        )

    def _load_initial_states(
        self,
    ) -> tuple[
        dict[tuple[int, int], BlockOutcome],
        list[tuple[int, int]],
        list[dict[str, Any]],
        dict[tuple[int, int], str],
    ]:
        outcomes: dict[tuple[int, int], BlockOutcome] = {}
        pending: list[tuple[int, int]] = []
        invalid_diagnostics: list[dict[str, Any]] = []
        invalid_reasons: dict[tuple[int, int], str] = {}
        for row, col in self.config.iter_block_indices():
            loaded = self.store.load_block(row, col)
            if isinstance(loaded, LoadedBlock):
                outcomes[(row, col)] = BlockOutcome(
                    row=row,
                    col=col,
                    status="reused",
                    digest=loaded.record.digest,
                )
            elif isinstance(loaded, InvalidBlock):
                invalid_diagnostics.append(loaded.diagnostic())
                invalid_reasons[(row, col)] = loaded.reason
                pending.append((row, col))
            else:
                pending.append((row, col))
        return outcomes, pending, invalid_diagnostics, invalid_reasons

    def _interrupt_payload(
        self,
        outcomes: dict[tuple[int, int], BlockOutcome],
        pending: list[tuple[int, int]],
    ) -> dict[str, Any]:
        completed = [
            {"row": row, "col": col}
            for row, col in self.config.iter_block_indices()
            if outcomes.get((row, col)) is not None
            and outcomes[(row, col)].status in {"reused", "recomputed"}
        ]
        missing = [{"row": row, "col": col} for row, col in pending]
        return {
            "reason": "graceful_signal",
            "exit_code": 75,
            "total_blocks": self.config.block_count,
            "completed_blocks": completed,
            "missing_blocks": missing,
        }

    def render_all(
        self,
        interrupt_event: threading.Event | None = None,
        block_delay_ms: int = 0,
        reference: np.ndarray | None = None,
    ) -> RenderResult:
        stats = self._ensure_normalization()
        (
            outcomes,
            pending,
            invalid_diagnostics,
            invalid_reasons,
        ) = self._load_initial_states()
        if interrupt_event is not None and interrupt_event.is_set() and pending:
            self.store.write_interrupt(self._interrupt_payload(outcomes, pending))
            raise RenderInterrupted(self._interrupt_payload(outcomes, pending))

        effective_concurrency = max(
            1, min(self.config.concurrency_limit, self.config.block_count)
        )
        per_block_pixels = self.config.flight_pixels_per_block
        in_flight: dict[Future[np.ndarray], tuple[int, int, int]] = {}
        used_pixels = 0
        peak_pixels = 0
        first_error: BaseException | None = None
        first_error_block: tuple[int, int] | None = None
        interrupted = False

        executor = ThreadPoolExecutor(
            max_workers=effective_concurrency,
            thread_name_prefix="relief-block",
        )
        try:
            while pending or in_flight:
                if interrupt_event is not None and interrupt_event.is_set():
                    interrupted = True
                    # Already-submitted blocks finish at their next boundary.

                while (
                    pending
                    and not interrupted
                    and len(in_flight) < effective_concurrency
                ):
                    if interrupt_event is not None and interrupt_event.is_set():
                        interrupted = True
                        break
                    row, col = pending[0]
                    if used_pixels + per_block_pixels > self.config.memory_budget_pixels:
                        break
                    pending.pop(0)
                    future = executor.submit(
                        _compute_block,
                        self.config,
                        stats,
                        row,
                        col,
                        block_delay_ms,
                    )
                    in_flight[future] = (row, col, per_block_pixels)
                    used_pixels += per_block_pixels
                    peak_pixels = max(peak_pixels, used_pixels)

                if (
                    pending
                    and not in_flight
                    and used_pixels + per_block_pixels
                    > self.config.memory_budget_pixels
                ):
                    raise BusinessError(
                        "E_BUDGET",
                        "no block fits within the configured memory budget",
                        {"memory_budget_pixels": self.config.memory_budget_pixels},
                    )

                if not in_flight:
                    if interrupted:
                        break
                    continue

                done, _ = wait(
                    tuple(in_flight.keys()),
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                for future in sorted(
                    done, key=lambda item: in_flight[item][:2]
                ):
                    row, col, cost = in_flight.pop(future)
                    used_pixels -= cost
                    try:
                        gray = future.result()
                    except Exception as exc:
                        if first_error is None:
                            first_error = exc
                            first_error_block = (row, col)
                        interrupted = True
                        continue
                    started = time.perf_counter()
                    record = self._persist_computed_block(row, col, gray)
                    duration_ms = (time.perf_counter() - started) * 1000.0
                    outcomes[(row, col)] = BlockOutcome(
                        row=row,
                        col=col,
                        status="recomputed",
                        digest=record.digest,
                        invalid_reason=invalid_reasons.get((row, col)),
                        duration_ms=duration_ms,
                    )
                    if interrupt_event is not None and interrupt_event.is_set():
                        interrupted = True

                if first_error is not None:
                    for future in in_flight:
                        future.cancel()
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if first_error is not None:
            details: dict[str, Any] = {
                "row": first_error_block[0] if first_error_block else None,
                "col": first_error_block[1] if first_error_block else None,
                "error_type": type(first_error).__name__,
                "error": str(first_error),
            }
            if isinstance(first_error, BusinessError):
                details.update(first_error.details)
            raise BusinessError(
                "E_RENDER", "a block failed; remaining work was cancelled", details
            ) from first_error

        if interrupted:
            payload = self._interrupt_payload(outcomes, pending)
            self.store.write_interrupt(payload)
            raise RenderInterrupted(payload)

        # A close-of-run validation pass closes the gap between a worker result
        # and a persisted file being externally replaced or removed.
        repair_invalid: list[InvalidBlock] = []
        for row, col in self.config.iter_block_indices():
            loaded = self.store.load_block(row, col)
            if isinstance(loaded, InvalidBlock):
                repair_invalid.append(loaded)
            elif isinstance(loaded, LoadedBlock):
                previous = outcomes.get((row, col))
                outcomes[(row, col)] = BlockOutcome(
                    row=row,
                    col=col,
                    status=previous.status if previous is not None else "reused",
                    digest=loaded.record.digest,
                    invalid_reason=(
                        previous.invalid_reason if previous is not None else None
                    ),
                    duration_ms=previous.duration_ms if previous is not None else None,
                )
            else:
                repair_invalid.append(
                    InvalidBlock(row, col, "missing", "block disappeared after render")
                )
        for invalid in repair_invalid:
            invalid_diagnostics.append(invalid.diagnostic())
            gray = _compute_block(
                self.config, stats, invalid.row, invalid.col, block_delay_ms
            )
            record = self._persist_computed_block(invalid.row, invalid.col, gray)
            outcomes[(invalid.row, invalid.col)] = BlockOutcome(
                row=invalid.row,
                col=invalid.col,
                status="recomputed",
                digest=record.digest,
                invalid_reason=invalid.reason,
            )
        for row, col in self.config.iter_block_indices():
            loaded = self.store.load_block(row, col)
            if not isinstance(loaded, LoadedBlock):
                reason = loaded.reason if isinstance(loaded, InvalidBlock) else "missing"
                raise BusinessError(
                    "E_DIGEST",
                    "a block remains invalid after digest fallback",
                    {"row": row, "col": col, "reason": reason},
                )

        mosaic = np.empty(
            (self.config.height, self.config.width), dtype=np.uint16
        )
        for row, col in self.config.iter_block_indices():
            loaded = self.store.load_block(row, col)
            if not isinstance(loaded, LoadedBlock):
                raise BusinessError(
                    "E_DIGEST",
                    "block unavailable during assembly",
                    {"row": row, "col": col},
                )
            x0, y0, width, height = self.config.block_bounds(row, col)
            mosaic[y0 : y0 + height, x0 : x0 + width] = loaded.gray

        if reference is None:
            reference_source = "direct_full_generation"
            reference_gray = generate_full_gray(self.config)
        else:
            reference_source = "provided_reference"
            reference_gray = np.asarray(reference)
            if reference_gray.shape != mosaic.shape:
                raise BusinessError(
                    "E_REFERENCE",
                    "reference shape does not match the configured canvas",
                    {
                        "reference_shape": list(reference_gray.shape),
                        "canvas_shape": list(mosaic.shape),
                    },
                )

        mismatch = _first_mismatch(mosaic, reference_gray, self.config)
        difference = np.abs(
            mosaic.astype(np.int64) - reference_gray.astype(np.int64)
        )
        max_reference_difference = int(difference.max(initial=0))
        seam = _seam_metrics(mosaic, self.config)
        ordered_outcomes = [
            outcomes[(row, col)].to_dict()
            for row, col in self.config.iter_block_indices()
        ]
        recomputed_count = sum(
            1 for outcome in outcomes.values() if outcome.status == "recomputed"
        )
        reused_count = sum(
            1 for outcome in outcomes.values() if outcome.status == "reused"
        )
        invalid_diagnostics = _unique_diagnostics(invalid_diagnostics)
        report: dict[str, Any] = {
            "ok": mismatch is None,
            "version": self.config.version,
            "canvas": {
                "width": self.config.width,
                "height": self.config.height,
            },
            "grid": {
                "rows": self.config.rows,
                "cols": self.config.cols,
                "block_width": self.config.block_width,
                "block_height": self.config.block_height,
            },
            "blocks": ordered_outcomes,
            "invalid_blocks": invalid_diagnostics,
            "recomputed_blocks": recomputed_count,
            "reused_blocks": reused_count,
            "total_blocks": self.config.block_count,
            "effective_concurrency": effective_concurrency,
            "memory_budget_pixels": self.config.memory_budget_pixels,
            "block_working_pixels": per_block_pixels,
            "peak_in_flight_pixels": peak_pixels,
            **seam,
            "reference_source": reference_source,
            "max_abs_diff_vs_full_reference": max_reference_difference,
            "first_mismatch": mismatch,
        }
        if mismatch is None:
            self.store.clear_interrupt()
        return RenderResult(mosaic=mosaic, report=report, first_mismatch=mismatch)


def _unique_diagnostics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (
            item.get("code"),
            item.get("row"),
            item.get("col"),
            item.get("reason"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
