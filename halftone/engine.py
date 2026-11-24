from __future__ import annotations

import copy
import io
import json
import shutil
import threading
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

import numpy as np

from .config import (
    DEFAULT_MEMORY_BUDGET,
    ValidatedJob,
    canonical_json_bytes,
    source_content_digest,
    validate_job,
    version_identifier,
)
from .errors import BusinessError, TerminationRequested
from .imaging import (
    first_consistency_mismatch,
    ink_tone_preview,
    moire_diagnostic,
    rational_resample,
    rational_scale,
    render_halftone,
    require_consistent,
)
from .atomicio import atomic_write_bytes
from .pngio import png_bytes, read_png, write_png, write_rgb_png
from .state import StateStore


class TerminationToken:
    def __init__(self, stop_after_completed: Optional[int] = None) -> None:
        self._condition = threading.Condition()
        self._requested = False
        self.reason: Optional[str] = None
        self.completed_tiles = 0
        self.stop_after_completed = stop_after_completed

    def request(self, reason: str = "requested") -> None:
        with self._condition:
            self._requested = True
            self.reason = reason
            self._condition.notify_all()

    @property
    def requested(self) -> bool:
        with self._condition:
            return self._requested

    def tile_completed(self) -> None:
        with self._condition:
            self.completed_tiles += 1
            if (
                self.stop_after_completed is not None
                and self.completed_tiles >= self.stop_after_completed
            ):
                self._requested = True
                self.reason = "tile-boundary-test-limit"
            self._condition.notify_all()

    def wait_if_requested(self, timeout: float = 0.05) -> bool:
        with self._condition:
            if self._requested:
                return True
            self._condition.wait(timeout=timeout)
            return self._requested


class MemoryBudget:
    def __init__(self, total_bytes: int = DEFAULT_MEMORY_BUDGET) -> None:
        if not isinstance(total_bytes, int) or isinstance(total_bytes, bool) or total_bytes <= 0:
            raise BusinessError(
                "E_RESOURCE", "memory_budget_bytes must be a positive integer"
            )
        self.total_bytes = total_bytes
        self.used_bytes = 0
        self.peak_bytes = 0
        self.wait_count = 0
        self._condition = threading.Condition()

    def acquire(self, size_bytes: int, token: Optional[TerminationToken] = None) -> None:
        if size_bytes > self.total_bytes:
            raise BusinessError(
                "E_RESOURCE",
                "a single tile buffer exceeds memory_budget_bytes",
                {
                    "tile_bytes": size_bytes,
                    "memory_budget_bytes": self.total_bytes,
                },
            )
        with self._condition:
            waited = False
            while self.used_bytes + size_bytes > self.total_bytes:
                if token is not None and token.requested:
                    raise TerminationRequested()
                if not waited:
                    self.wait_count += 1
                    waited = True
                self._condition.wait(timeout=0.05)
            self.used_bytes += size_bytes
            self.peak_bytes = max(self.peak_bytes, self.used_bytes)

    def release(self, size_bytes: int) -> None:
        with self._condition:
            self.used_bytes = max(0, self.used_bytes - size_bytes)
            self._condition.notify_all()


class SourceLibrary:
    def __init__(
        self,
        manifest: Mapping[str, Mapping[str, Any]],
        samples_dir: str | Path,
    ) -> None:
        self.manifest = manifest
        self.samples_dir = Path(samples_dir)
        self._cache: Dict[str, np.ndarray] = {}

    def get(self, source_id: str) -> np.ndarray:
        if source_id in self._cache:
            return self._cache[source_id]
        entry = self.manifest.get(source_id)
        if entry is None:
            raise BusinessError(
                "E_SOURCE",
                "source_id is not present in image manifest",
                {"source_id": source_id},
            )
        file_name = entry.get("file")
        if not isinstance(file_name, str):
            raise BusinessError(
                "E_SOURCE", "source manifest entry is missing file", {"source_id": source_id}
            )
        path = self.samples_dir / file_name
        gray = read_png(path)
        expected_digest = entry.get("content_sha256")
        if expected_digest and source_content_digest(gray) != expected_digest:
            raise BusinessError(
                "E_SOURCE",
                "grayscale source content digest does not match manifest",
                {"source_id": source_id, "path": str(path)},
            )
        width = entry.get("width")
        height = entry.get("height")
        if width is not None and height is not None:
            if gray.shape != (int(height), int(width)):
                raise BusinessError(
                    "E_SOURCE",
                    "source image dimensions do not match manifest",
                    {
                        "source_id": source_id,
                        "manifest": [width, height],
                        "actual": [gray.shape[1], gray.shape[0]],
                    },
                )
        self._cache[source_id] = gray
        return gray


def tile_byte_count(job: ValidatedJob) -> int:
    return job.tile_width * job.tile_height


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def process_validated_job(
    job: ValidatedJob,
    source_gray: np.ndarray,
    *,
    state: StateStore,
    out_dir: str | Path,
    workers: int = 2,
    budget: Optional[MemoryBudget] = None,
    token: Optional[TerminationToken] = None,
    tile_delay_ms: int = 0,
) -> Dict[str, Any]:
    if workers < 1:
        raise BusinessError("E_RESOURCE", "workers must be at least 1")
    budget = budget or MemoryBudget(DEFAULT_MEMORY_BUDGET)
    token = token or TerminationToken()
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    output_gray = rational_resample(
        source_gray, job.output_width, job.output_height
    )
    version_id = version_identifier(job, source_gray)
    reusable, archived_from_version = state.prepare_version(job, version_id)
    total_tiles = job.tile_cols * job.tile_rows
    pending = [index for index in range(total_tiles) if index not in reusable]
    tile_bytes = tile_byte_count(job)
    computed_records: List[Dict[str, Any]] = []

    def run_tile(index: int) -> Dict[str, Any]:
        try:
            if tile_delay_ms > 0:
                time.sleep(tile_delay_ms / 1000.0)
            tile_y, tile_x = divmod(index, job.tile_cols)
            x0 = tile_x * job.tile_width
            y0 = tile_y * job.tile_height
            source_tile = output_gray[
                y0 : y0 + job.tile_height,
                x0 : x0 + job.tile_width,
            ]
            pixels = render_halftone(
                source_tile, job, origin_x=x0, origin_y=y0
            )
            state.write_tile(job, version_id, index, pixels)
            return {"index": index, "bytes": int(pixels.nbytes)}
        finally:
            budget.release(tile_bytes)

    in_flight = set()

    def harvest_one_completed() -> None:
        if not in_flight:
            return
        done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
        for future in done:
            in_flight.remove(future)
            record = future.result()
            computed_records.append(record)
            token.tile_completed()

    try:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"ht-{job.job_id[:24]}",
        ) as executor:
            for index in pending:
                if token.requested:
                    raise TerminationRequested(len(computed_records))
                while len(in_flight) >= workers:
                    harvest_one_completed()
                if token.requested:
                    raise TerminationRequested(len(computed_records))

                budget.acquire(tile_bytes, token)
                if token.requested:
                    budget.release(tile_bytes)
                    raise TerminationRequested(len(computed_records))
                try:
                    future = executor.submit(run_tile, index)
                except BaseException:
                    budget.release(tile_bytes)
                    raise
                in_flight.add(future)

            while in_flight:
                harvest_one_completed()
    except TerminationRequested:
        # The ThreadPoolExecutor context exit waits for all already-running
        # tiles. Their finally blocks release memory and successful atomic
        # writes are recoverable on the next invocation.
        raise

    if token.requested:
        raise TerminationRequested(len(computed_records))

    assembled = np.full((job.output_height, job.output_width), -1, dtype=np.int16)
    for index in range(total_tiles):
        pixels = state.read_tile_pixels(job, version_id, index)
        tile_y, tile_x = divmod(index, job.tile_cols)
        x0 = tile_x * job.tile_width
        y0 = tile_y * job.tile_height
        assembled[
            y0 : y0 + job.tile_height,
            x0 : x0 + job.tile_width,
        ] = pixels

    reference = render_halftone(output_gray, job, origin_x=0, origin_y=0)
    mismatch = first_consistency_mismatch(assembled, reference, job)
    if mismatch is not None:
        raise BusinessError(
            "E_CONSIST",
            "assembled tiles differ from full-coordinate render",
            {"first_mismatch": mismatch},
        )

    final_image = assembled.astype(np.uint8)
    output_png = out_path / f"{job.job_id}.png"
    write_png(output_png, final_image)
    ink_preview_path = out_path / f"{job.job_id}_ink.png"
    write_rgb_png(ink_preview_path, ink_tone_preview(final_image, job.ink_rgb))

    tile_bundle_path = out_path / f"{job.job_id}_tiles.zip"
    bundle_buffer = io.BytesIO()
    with zipfile.ZipFile(
        bundle_buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for index in range(total_tiles):
            tile_y, tile_x = divmod(index, job.tile_cols)
            x0 = tile_x * job.tile_width
            y0 = tile_y * job.tile_height
            tile_pixels = final_image[
                y0 : y0 + job.tile_height,
                x0 : x0 + job.tile_width,
            ]
            tile_info = zipfile.ZipInfo(
                f"{job.job_id}/tile_y{tile_y:03d}_x{tile_x:03d}"
                f"_offset{y0:05d}_{x0:05d}.png"
            )
            tile_info.date_time = (1980, 1, 1, 0, 0, 0)
            tile_info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(tile_info, png_bytes(tile_pixels))
    atomic_write_bytes(tile_bundle_path, bundle_buffer.getvalue())

    diagnostic = moire_diagnostic(final_image, job)
    source_h, source_w = source_gray.shape
    ratio_x = rational_scale(source_w, job.output_width)
    ratio_y = rational_scale(source_h, job.output_height)
    diagnostic["resample_ratio_x"] = f"{ratio_x.numerator}/{ratio_x.denominator}"
    diagnostic["resample_ratio_y"] = f"{ratio_y.numerator}/{ratio_y.denominator}"
    diagnostic["resampled"] = (source_w, source_h) != (
        job.output_width,
        job.output_height,
    )

    return {
        "ok": True,
        "job_id": job.job_id,
        "version_id": version_id,
        "archived_from_version": archived_from_version,
        "output_png": _display_path(output_png),
        "ink_preview_png": _display_path(ink_preview_path),
        "tile_bundle": _display_path(tile_bundle_path),
        "tile_count": total_tiles,
        "computed_tiles": len(computed_records),
        "reused_tiles": len(reusable),
        "workers": workers,
        "scheduling_order": "job_id ascending; tile_index row-major ascending",
        "tile_bytes": tile_bytes,
        "mesh_pitch_px": job.mesh_pitch_px,
        "phase_formula": (
            "p=dpi/lpi; u=(x+0.5)cos(a)+(y+0.5)sin(a); "
            "v=-(x+0.5)sin(a)+(y+0.5)cos(a); "
            "dx=(u/p-floor(u/p))-0.5; dy=(v/p-floor(v/p))-0.5"
        ),
        "consistency": {
            "matched": True,
            "checked_pixels": int(job.output_width * job.output_height),
            "first_mismatch": None,
        },
        "moire": diagnostic,
        "ink_tone": list(job.ink_rgb),
    }


def run_job_dict(
    raw_job: Mapping[str, Any],
    sources: SourceLibrary,
    **kwargs: Any,
) -> Dict[str, Any]:
    raw_job_id = raw_job.get("job_id") if isinstance(raw_job, Mapping) else None
    try:
        job = validate_job(raw_job)
        source_gray = sources.get(job.source_id)
        return process_validated_job(job, source_gray, **kwargs)
    except TerminationRequested:
        raise
    except BusinessError as exc:
        return {
            "ok": False,
            "job_id": raw_job_id if isinstance(raw_job_id, str) else None,
            **exc.to_dict(),
        }
    except Exception as exc:  # defensive boundary: one job must not kill batch
        error = BusinessError(
            "E_INTERNAL",
            "unexpected processing failure",
            {"exception_type": type(exc).__name__, "message": str(exc)},
        )
        return {
            "ok": False,
            "job_id": raw_job_id if isinstance(raw_job_id, str) else None,
            **error.to_dict(),
        }


def apply_anomaly(raw_job: Dict[str, Any], anomaly: Mapping[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(raw_job)
    result["job_id"] = anomaly["job_id"]
    mutations = anomaly.get("mutations", {})
    if not isinstance(mutations, Mapping):
        raise BusinessError("E_INJECTION", "anomaly mutations must be an object")
    for key, value in mutations.items():
        if value == "__DELETE__":
            result.pop(key, None)
        else:
            result[key] = value
    for key in anomaly.get("remove", []):
        result.pop(str(key), None)
    return result


def prepare_digest_corruption(
    anomaly: Mapping[str, Any],
    base_job: Mapping[str, Any],
    sources: SourceLibrary,
    state: StateStore,
) -> str:
    injected_raw = apply_anomaly(dict(base_job), anomaly)
    job = validate_job(injected_raw)
    shutil.rmtree(state.job_dir(job.job_id), ignore_errors=True)
    source_gray = sources.get(job.source_id)
    output_gray = rational_resample(source_gray, job.output_width, job.output_height)
    version_id = version_identifier(job, source_gray)
    state.prepare_version(job, version_id)
    index = int(anomaly.get("tile_index", 0))
    tile_y, tile_x = divmod(index, job.tile_cols)
    x0 = tile_x * job.tile_width
    y0 = tile_y * job.tile_height
    pixels = render_halftone(
        output_gray[y0 : y0 + job.tile_height, x0 : x0 + job.tile_width],
        job,
        origin_x=x0,
        origin_y=y0,
    )
    state.write_tile(job, version_id, index, pixels)
    corrupted_path = state.corrupt_digest_for_injection(job.job_id, index)
    return str(corrupted_path)


def run_batch(
    jobs: Sequence[Mapping[str, Any]],
    sources: SourceLibrary,
    *,
    state: StateStore,
    out_dir: str | Path,
    workers: int = 2,
    memory_budget_bytes: int = DEFAULT_MEMORY_BUDGET,
    token: Optional[TerminationToken] = None,
    tile_delay_ms: int = 0,
) -> Dict[str, Any]:
    budget = MemoryBudget(memory_budget_bytes)
    token = token or TerminationToken()
    ordered_jobs = sorted(jobs, key=lambda item: str(item.get("job_id", "")))
    outcomes: List[Dict[str, Any]] = []
    terminated: Optional[TerminationRequested] = None

    for raw_job in ordered_jobs:
        if token.requested:
            break
        try:
            outcome = run_job_dict(
                raw_job,
                sources,
                state=state,
                out_dir=out_dir,
                workers=workers,
                budget=budget,
                token=token,
                tile_delay_ms=tile_delay_ms,
            )
        except TerminationRequested as exc:
            terminated = exc
            break
        outcomes.append(outcome)

    successful = sum(1 for item in outcomes if item.get("ok"))
    failed = len(outcomes) - successful
    report: Dict[str, Any] = {
        "schema": "halftone-batch-report/v1",
        "status": "terminated" if terminated is not None or token.requested else "completed",
        "exit_code": 20 if terminated is not None or token.requested else 0,
        "successful_jobs": successful,
        "failed_jobs": failed,
        "processed_jobs": len(outcomes),
        "workers": workers,
        "memory_budget_bytes": budget.total_bytes,
        "memory_peak_bytes": budget.peak_bytes,
        "memory_wait_count": budget.wait_count,
        "jobs": outcomes,
    }
    if terminated is not None:
        report["termination"] = {
            "reason": token.reason,
            "completed_tiles": token.completed_tiles,
            "exit_code": 20,
        }
    return report
