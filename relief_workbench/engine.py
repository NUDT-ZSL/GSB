from __future__ import annotations

import hashlib
import json
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .config import WorkbenchConfig
from .errors import WorkbenchError
from .png_io import atomic_write_bytes, write_png_if_requested
from .renderer import GlobalStats, ReliefRenderer, TileSpec
from .storage import StateStore, TileCacheRecord, TileInvalid

WORKING_BYTES_PER_TILE_PIXEL = 80


class _TerminationRequested(Exception):
    def __init__(self, signal_number: int, exit_code: int) -> None:
        super().__init__(f"termination requested by signal {signal_number}")
        self.signal_number = signal_number
        self.exit_code = exit_code


@dataclass(frozen=True)
class PreviewResult:
    spec: TileSpec
    pixels: np.ndarray
    cache_hit: bool
    output_path: Path


def _sha256_array(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


class WorkbenchEngine:
    """Deterministic, cache-aware tile rendering orchestrator."""

    def __init__(
        self,
        config: WorkbenchConfig,
        state_dir: str | Path,
        output_dir: str | Path,
        *,
        renderer: ReliefRenderer | None = None,
        tile_delay_seconds: float = 0.0,
    ) -> None:
        self.config = config
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.renderer = renderer or ReliefRenderer(config)
        self.store = StateStore(self.state_dir, config)
        self.stats: GlobalStats | None = None
        self._prepared = False
        self._stop_event = threading.Event()
        self._signal_number: int | None = None
        self._tile_delay_seconds = max(0.0, tile_delay_seconds)
        self._digest_events: list[dict[str, Any]] = []
        self._rendered_this_run: list[TileSpec] = []
        self._cache_hits_this_run: list[TileSpec] = []
        self.peak_in_flight_bytes = 0
        self.max_observed_workers = 0

    @property
    def version(self) -> str:
        return self.config.version

    @property
    def digest_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._digest_events)

    def request_termination(self, signal_number: int = signal.SIGTERM) -> None:
        if self._signal_number is None:
            self._signal_number = int(signal_number)
        self._stop_event.set()

    def termination_requested(self) -> bool:
        return self._stop_event.is_set()

    def _exit_code(self) -> int:
        signal_number = self._signal_number
        if signal_number == getattr(signal, "SIGINT", None):
            return 130
        if signal_number == getattr(signal, "SIGTERM", None):
            return 143
        if signal_number == getattr(signal, "SIGBREAK", None):
            return 143
        return 1

    def _raise_if_termination_requested(self) -> None:
        if self._stop_event.is_set():
            if self._signal_number is None:
                self._signal_number = 15
            raise _TerminationRequested(
                self._signal_number,
                self._exit_code(),
            )

    def _reset_run_counters(self) -> None:
        self._digest_events = []
        self._rendered_this_run = []
        self._cache_hits_this_run = []
        self.peak_in_flight_bytes = 0
        self.max_observed_workers = 0

    def prepare(self) -> None:
        if self._prepared:
            return
        self.store.prepare()
        if self.store.purged:
            self._purge_outputs()
        self.stats = self.store.load_stats()
        self._prepared = True

    def _ensure_output_dir(self) -> None:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkbenchError(
                "E_OUTPUT_WRITE",
                "unable to create output directory",
                {"path": str(self.output_dir), "reason": str(exc)},
            ) from exc

    def _purge_outputs(self) -> None:
        if not self.output_dir.exists():
            return
        patterns = (
            "full.png",
            "report.json",
            "tiles_manifest.json",
            "preview_r*.png",
        )
        for pattern in patterns:
            for path in self.output_dir.glob(pattern):
                if path.is_file():
                    try:
                        path.unlink()
                    except OSError:
                        pass
        tiles_dir = self.output_dir / "tiles"
        if tiles_dir.exists():
            for path in tiles_dir.glob("*.png"):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _ensure_stats(
        self, should_stop: Callable[[], bool] | None = None
    ) -> GlobalStats:
        self.prepare()
        if self.stats is not None:
            return self.stats
        self._raise_if_termination_requested()
        try:
            stats = self.renderer.compute_global_stats(
                should_stop=lambda: self._stop_event.is_set()
                or (should_stop is not None and should_stop())
            )
        except WorkbenchError as exc:
            if exc.code != "E_INTERRUPTED":
                raise
            if self._signal_number is None:
                self._signal_number = 15
            raise _TerminationRequested(
                self._signal_number,
                self._exit_code(),
            ) from exc
        self._raise_if_termination_requested()
        self.stats = stats
        self.store.save_stats(stats)
        return stats

    def _load_cached(self, spec: TileSpec) -> TileCacheRecord | None:
        path = self.store.tile_path(spec)
        if not path.exists():
            return None
        try:
            return self.store.read_tile(spec)
        except TileInvalid as exc:
            self._digest_events.append(
                {
                    "code": "E_DIGEST",
                    "phase": "cache_load",
                    "tile": spec.to_dict(),
                    "reason": exc.reason,
                    "details": exc.details,
                }
            )
            self.store.delete_tile(spec)
            return None

    def _record_cache_hit(self, spec: TileSpec) -> None:
        if spec not in self._cache_hits_this_run:
            self._cache_hits_this_run.append(spec)

    def _record_rendered(self, spec: TileSpec) -> None:
        if spec not in self._rendered_this_run:
            self._rendered_this_run.append(spec)

    def _tile_output_path(self, spec: TileSpec) -> Path:
        return self.output_dir / "tiles" / f"tile_{spec.name}.png"

    def _preview_output_path(self, spec: TileSpec) -> Path:
        return self.output_dir / f"preview_{spec.name}.png"

    def _render_and_store(
        self, spec: TileSpec, stats: GlobalStats
    ) -> tuple[TileSpec, np.ndarray, str]:
        if self._tile_delay_seconds > 0.0:
            time.sleep(self._tile_delay_seconds)
        self._raise_if_termination_requested()
        pixels = self.renderer.render_tile(spec, stats)
        record = self.store.write_tile(spec, pixels)
        self._ensure_output_dir()
        write_png_if_requested(self._tile_output_path(spec), pixels)
        return spec, pixels.copy(), record.content_sha256

    def preview_tile(self, row: int | None = None, col: int | None = None) -> PreviewResult:
        try:
            self.prepare()
            if row is None:
                row = self.config.preview_tile[0]
            if col is None:
                col = self.config.preview_tile[1]
            spec = self.renderer.spec(row, col)
            stats = self._ensure_stats()
            self._raise_if_termination_requested()
            cached = self._load_cached(spec)
            if cached is not None:
                pixels = cached.pixels
                cache_hit = True
                self._record_cache_hit(spec)
            else:
                pixels = self.renderer.render_tile(spec, stats)
                self.store.write_tile(spec, pixels)
                cache_hit = False
                self._record_rendered(spec)
            self._ensure_output_dir()
            preview_path = self._preview_output_path(spec)
            write_png_if_requested(preview_path, pixels)
            write_png_if_requested(self._tile_output_path(spec), pixels)
            return PreviewResult(
                spec=spec,
                pixels=pixels.copy(),
                cache_hit=cache_hit,
                output_path=preview_path,
            )
        except _TerminationRequested as exc:
            self._write_interrupt_marker(exc)
            raise WorkbenchError(
                "E_INTERRUPTED",
                "rendering was interrupted at a tile boundary",
                {"signal": exc.signal_number, "exit_code": exc.exit_code},
                exit_code=exc.exit_code,
            ) from exc

    def _scheduled_render(
        self,
        specs: list[TileSpec],
        stats: GlobalStats,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[int, np.ndarray]:
        if not specs:
            return {}
        budget = self.config.memory_budget_bytes
        worker_limit = self.config.concurrency
        executor = ThreadPoolExecutor(
            max_workers=worker_limit, thread_name_prefix="relief-tile"
        )
        pending = [spec for spec in specs]
        next_index = 0
        in_flight = 0
        futures: dict[Future[tuple[TileSpec, np.ndarray, str]], TileSpec] = {}
        results: dict[int, np.ndarray] = {}
        first_error: BaseException | None = None
        termination: _TerminationRequested | None = None

        try:
            while next_index < len(pending) or futures:
                if should_stop is not None and should_stop():
                    self.request_termination(signal.SIGTERM)
                if self._stop_event.is_set() and not futures:
                    raise _TerminationRequested(
                        self._signal_number
                        if self._signal_number is not None
                        else 15,
                        self._exit_code(),
                    )

                while (
                    not self._stop_event.is_set()
                    and next_index < len(pending)
                    and len(futures) < worker_limit
                ):
                    spec = pending[next_index]
                    cost = spec.pixel_count * WORKING_BYTES_PER_TILE_PIXEL
                    if cost > budget:
                        raise WorkbenchError(
                            "E_MEMORY_BUDGET",
                            "one tile exceeds the configured in-flight pixel budget",
                            {
                                "tile": spec.to_dict(),
                                "tile_cost_bytes": cost,
                                "memory_budget_bytes": budget,
                            },
                        )
                    if in_flight + cost > budget:
                        break
                    next_index += 1
                    future = executor.submit(
                        self._render_and_store, spec, stats
                    )
                    futures[future] = spec
                    in_flight += cost
                    if in_flight > self.peak_in_flight_bytes:
                        self.peak_in_flight_bytes = in_flight
                    self.max_observed_workers = max(
                        self.max_observed_workers, len(futures)
                    )

                if not futures:
                    if next_index < len(pending):
                        spec = pending[next_index]
                        cost = spec.pixel_count * WORKING_BYTES_PER_TILE_PIXEL
                        raise WorkbenchError(
                            "E_MEMORY_BUDGET",
                            "no tile can fit within the configured in-flight pixel budget",
                            {
                                "tile": spec.to_dict(),
                                "tile_cost_bytes": cost,
                                "memory_budget_bytes": budget,
                            },
                        )
                    break

                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    spec = futures.pop(future)
                    cost = spec.pixel_count * WORKING_BYTES_PER_TILE_PIXEL
                    in_flight -= cost
                    if first_error is not None:
                        if not future.cancelled():
                            future.exception()
                        continue
                    try:
                        _spec, pixels, _digest = future.result()
                    except BaseException as exc:
                        first_error = exc
                        for queued in futures:
                            queued.cancel()
                        continue
                    key = spec.row * self.config.grid_cols + spec.col
                    results[key] = pixels

                if first_error is not None:
                    break
                if self._stop_event.is_set():
                    termination = _TerminationRequested(
                        self._signal_number
                        if self._signal_number is not None
                        else 15,
                        self._exit_code(),
                    )
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if first_error is not None:
            if isinstance(first_error, WorkbenchError):
                raise first_error
            if isinstance(first_error, _TerminationRequested):
                raise first_error
            raise WorkbenchError(
                "E_RENDER",
                "a tile render failed; pending tiles were cancelled",
                {
                    "error_type": type(first_error).__name__,
                    "reason": str(first_error),
                },
            ) from first_error
        if termination is not None:
            raise termination
        return results

    def _assemble_from_disk(
        self, specs: Iterable[TileSpec]
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        image = np.empty(
            (self.config.height, self.config.width), dtype=np.uint8
        )
        failures: list[dict[str, Any]] = []
        for spec in specs:
            try:
                record = self.store.read_tile(spec)
            except TileInvalid as exc:
                failures.append(
                    {
                        **spec.to_dict(),
                        "reason": exc.reason,
                        **exc.details,
                    }
                )
                continue
            image[
                spec.origin_y : spec.end_y, spec.origin_x : spec.end_x
            ] = record.pixels
        return image, failures

    def _repair_tiles(
        self,
        failures: list[dict[str, Any]],
        stats: GlobalStats,
        phase: str,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        failed_specs: list[TileSpec] = []
        for failure in failures:
            spec = self.renderer.spec(
                int(failure["row"]), int(failure["col"])
            )
            failed_specs.append(spec)
            self._digest_events.append(
                {
                    "code": "E_DIGEST",
                    "phase": phase,
                    "tile": spec.to_dict(),
                    "reason": failure.get("reason", "invalid_tile"),
                    "details": {
                        key: value
                        for key, value in failure.items()
                        if key not in {"row", "col"}
                    },
                }
            )
            self.store.delete_tile(spec)
        self._scheduled_render(failed_specs, stats, should_stop=should_stop)
        for spec in failed_specs:
            self._record_rendered(spec)

    def _digest_repair_cycle(
        self,
        specs: tuple[TileSpec, ...],
        stats: GlobalStats,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        failures = self.store.verify_all(specs)
        if not failures:
            return
        self._repair_tiles(
            failures,
            stats,
            phase="post_render_verify",
            should_stop=should_stop,
        )
        remaining = self.store.verify_all(specs)
        if remaining:
            first = remaining[0]
            raise WorkbenchError(
                "E_DIGEST",
                "tile digest verification failed after fallback recomputation",
                {
                    "failure_count": len(remaining),
                    "first": first,
                    "events": list(self._digest_events),
                },
            )

    def _classify_mismatch(
        self,
        assembled_value: int,
        reference_value: int,
        spec: TileSpec,
        stats: GlobalStats,
        x: int,
        y: int,
    ) -> str:
        if abs(assembled_value - reference_value) <= 1:
            return "quantization"
        diagnostics = self.renderer.diagnostics(spec, stats)
        local_x = x - spec.origin_x
        local_y = y - spec.origin_y
        if (
            assembled_value
            == int(diagnostics.local_normalization[local_y, local_x])
        ):
            return "local_normalization"
        if (
            assembled_value
            == int(diagnostics.local_gradient[local_y, local_x])
        ):
            return "local_gradient_boundary"
        if assembled_value == int(diagnostics.local_phase[local_y, local_x]):
            return "local_coordinate_phase"
        if (
            x in (spec.origin_x, spec.end_x - 1)
            or y in (spec.origin_y, spec.end_y - 1)
        ):
            return "tile_boundary_phase_or_gradient"
        return "content_or_metadata"

    def _compare_assembled(
        self,
        assembled: np.ndarray,
        reference: np.ndarray,
        stats: GlobalStats,
    ) -> dict[str, Any]:
        difference = np.abs(
            assembled.astype(np.int16) - reference.astype(np.int16)
        )
        exceeds = difference > self.config.normalization_tolerance
        mismatch_count = int(np.count_nonzero(exceeds))
        max_error = int(np.max(difference))
        if mismatch_count:
            y, x = np.argwhere(exceeds)[0]
            x = int(x)
            y = int(y)
            spec = self.renderer.spec_for_pixel(x, y)
            assembled_value = int(assembled[y, x])
            reference_value = int(reference[y, x])
            cause = self._classify_mismatch(
                assembled_value,
                reference_value,
                spec,
                stats,
                x,
                y,
            )
            mismatch = {
                "x": x,
                "y": y,
                "tile": spec.to_dict(),
                "assembled": assembled_value,
                "reference": reference_value,
                "abs_error": abs(assembled_value - reference_value),
                "cause": cause,
            }
        else:
            mismatch = None
        return {
            "ok": mismatch is None,
            "max_abs_error": max_error,
            "mismatch_count": mismatch_count,
            "mismatch": mismatch,
            "assembled_sha256": _sha256_array(assembled),
            "reference_sha256": _sha256_array(reference),
        }

    def _write_tiles_manifest(
        self, specs: Iterable[TileSpec]
    ) -> Path:
        entries = []
        for spec in specs:
            record = self.store.read_tile(spec)
            entries.append(
                {
                    **spec.to_dict(),
                    "content_sha256": record.content_sha256,
                    "version": record.version,
                }
            )
        payload = {
            "schema_version": 1,
            "version": self.version,
            "tiles": entries,
        }
        path = self.output_dir / "tiles_manifest.json"
        atomic_write_bytes(
            path,
            json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"),
        )
        return path

    def _write_all_tile_pngs(self, specs: Iterable[TileSpec]) -> None:
        self._ensure_output_dir()
        for spec in specs:
            record = self.store.read_tile(spec)
            write_png_if_requested(self._tile_output_path(spec), record.pixels)

    def _base_report(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "config": self.config.public_dict(),
            "state_dir": str(self.state_dir),
            "output_dir": str(self.output_dir),
            "global_stats": self.stats.to_dict() if self.stats is not None else None,
            "cache": {
                "reused_tiles": len(self._cache_hits_this_run),
                "rendered_tiles": len(self._rendered_this_run),
                "reused": [
                    {"row": spec.row, "col": spec.col}
                    for spec in self._cache_hits_this_run
                ],
                "rendered": [
                    {"row": spec.row, "col": spec.col}
                    for spec in self._rendered_this_run
                ],
                "peak_in_flight_bytes": self.peak_in_flight_bytes,
                "max_observed_workers": self.max_observed_workers,
                "memory_budget_bytes": self.config.memory_budget_bytes,
            },
            "digest_repairs": list(self._digest_events),
        }

    def render_all(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self._reset_run_counters()
        try:
            self.prepare()
            if should_stop is not None and should_stop():
                self.request_termination(signal.SIGTERM)
            stats = self._ensure_stats(should_stop=should_stop)
            specs = self.renderer.specs
            assembled = np.empty(
                (self.config.height, self.config.width), dtype=np.uint8
            )
            cached_specs: list[TileSpec] = []
            pending_specs: list[TileSpec] = []
            for spec in specs:
                cached = self._load_cached(spec)
                if cached is None:
                    pending_specs.append(spec)
                    continue
                assembled[
                    spec.origin_y : spec.end_y, spec.origin_x : spec.end_x
                ] = cached.pixels
                cached_specs.append(spec)
                self._record_cache_hit(spec)

            self._raise_if_termination_requested()
            rendered = self._scheduled_render(
                pending_specs, stats, should_stop=should_stop
            )
            for spec in pending_specs:
                key = spec.row * self.config.grid_cols + spec.col
                if key in rendered:
                    assembled[
                        spec.origin_y : spec.end_y, spec.origin_x : spec.end_x
                    ] = rendered[key]
                    self._record_rendered(spec)

            self._raise_if_termination_requested()
            self._digest_repair_cycle(
                specs, stats, should_stop=should_stop
            )
            assembled, failures = self._assemble_from_disk(specs)
            if failures:
                self._repair_tiles(
                    failures,
                    stats,
                    phase="assembly_verify",
                    should_stop=should_stop,
                )
                assembled, failures = self._assemble_from_disk(specs)
                if failures:
                    raise WorkbenchError(
                        "E_DIGEST",
                        "tile verification failed after assembly fallback",
                        {"failure_count": len(failures), "first": failures[0]},
                    )

            self._raise_if_termination_requested()
            reference = self.renderer.render_full(stats)
            comparison = self._compare_assembled(assembled, reference, stats)
            self._ensure_output_dir()
            report = self._base_report()
            report.update(
                {
                    "ok": comparison["ok"],
                    "elapsed_seconds": time.monotonic() - started,
                    "tile_count": len(specs),
                    "completed_tiles": len(specs),
                    "preview_tile": list(self.config.preview_tile),
                    "comparison": comparison,
                    "previous_interruption": self.store.previous_interruption,
                }
            )
            report_path = self.output_dir / "report.json"
            if not comparison["ok"]:
                atomic_write_bytes(
                    report_path,
                    json.dumps(report, sort_keys=True, indent=2).encode("utf-8"),
                )
                mismatch = comparison["mismatch"]
                assert mismatch is not None
                raise WorkbenchError(
                    "E_CONSISTENCY",
                    "assembled tiles differ from the direct full-canvas render",
                    mismatch,
                )

            self._write_all_tile_pngs(specs)
            full_path = write_png_if_requested(
                self.output_dir / "full.png", assembled
            )
            tiles_manifest_path = self._write_tiles_manifest(specs)
            report["outputs"] = {
                "full_png": str(full_path),
                "tiles_dir": str(self.output_dir / "tiles"),
                "tiles_manifest": str(tiles_manifest_path),
                "report_json": str(report_path),
            }
            atomic_write_bytes(
                report_path,
                json.dumps(report, sort_keys=True, indent=2).encode("utf-8"),
            )
            return report
        except _TerminationRequested as exc:
            self._write_interrupt_marker(exc)
            raise WorkbenchError(
                "E_INTERRUPTED",
                "rendering was interrupted at a tile boundary",
                {"signal": exc.signal_number, "exit_code": exc.exit_code},
                exit_code=exc.exit_code,
            ) from exc

    def _write_interrupt_marker(self, exc: _TerminationRequested) -> None:
        specs = self.renderer.specs
        completed: list[TileSpec] = []
        for spec in specs:
            try:
                self.store.read_tile(spec)
            except TileInvalid:
                continue
            completed.append(spec)
        completed_keys = {(spec.row, spec.col) for spec in completed}
        remaining = [
            spec for spec in specs if (spec.row, spec.col) not in completed_keys
        ]
        try:
            self.store.write_interrupt_marker(
                signal_number=exc.signal_number,
                exit_code=exc.exit_code,
                completed=completed,
                remaining=remaining,
            )
        except OSError:
            pass
