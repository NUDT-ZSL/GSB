"""High-level orchestration: detect, recognize, cache, plan and query."""

from __future__ import annotations

from dataclasses import dataclass

from .cache import CacheEntry, RecognitionCache, build_cache_key
from .chunking import (
    ResourceObservation,
    assert_byte_identical,
    plan_chunk_size,
    run_chunked,
    run_whole_page,
)
from .detection import boxes_summary, detect_lines, detection_stats
from .errors import InvalidInputError, LatencyBudgetError
from .metrics import compute_metrics
from .recognition import (
    NormStats,
    RecognitionParams,
    collect_norm_stats,
    recognize_lines,
)
from .sample import PageSample
from .templates import TemplateLibrary
from .util import array_hash, content_hash, perf_counter_seconds, round6


@dataclass
class PageResult:
    page_id: str
    image_hash: str
    boxes: list
    norm_stats: NormStats
    whole_records: list
    chunk_records: list
    chunk_plan: dict
    metrics: dict
    detection_stats: dict

    def payload(self) -> dict:
        return {
            "page_id": self.page_id,
            "image_hash": self.image_hash,
            "boxes": [line.to_json() for line in self.boxes],
            "norm_stats": self.norm_stats.to_json(),
            "lines": self.whole_records,
            "chunk_plan": self.chunk_plan,
            "metrics": self.metrics,
            "detection_stats": self.detection_stats,
        }

    def result_hash(self) -> str:
        return content_hash(self.payload())


class OCREngine:
    def __init__(
        self,
        library: TemplateLibrary,
        params: RecognitionParams,
        cache: RecognitionCache | None = None,
    ):
        self.library = library
        self.params = params
        self.cache = cache or RecognitionCache()

    def process_page(
        self,
        page: PageSample,
        *,
        chunk_size: int,
        latency_budget_ms: float,
        memory_ceiling_bytes: int,
        use_cache: bool = True,
    ) -> tuple[PageResult, dict]:
        detections = detect_lines(page.image)
        norm = collect_norm_stats(page.image, detections)
        image_hash = array_hash(page.image)
        box_hash = boxes_summary(detections)

        # Explicit latency/memory/accuracy arbitration before decoding.
        if detections:
            mean_pixels = sum(
                (b.box[2] - b.box[0] + 1) * (b.box[3] - b.box[1] + 1)
                for b in detections
            ) / len(detections)
            first_box = detections[0].box
            mean_slots = max(1, (first_box[2] - first_box[0] + 1) // 12)
            mean_candidates = float(self.library.glyph_count * mean_slots)
        else:
            mean_pixels = 0.0
            mean_candidates = float(self.library.glyph_count)
        observation = ResourceObservation(
            line_count=len(detections),
            mean_line_pixels=float(mean_pixels),
            mean_match_candidates=float(mean_candidates or self.library.glyph_count),
            chunk_overhead_per_boundary=max(1.0, mean_pixels * 0.1),
            memory_bytes_per_line=float(mean_pixels) * 8.0,
        )
        plan = plan_chunk_size(
            observation,
            latency_budget_ms=latency_budget_ms,
            memory_ceiling_bytes=memory_ceiling_bytes,
        )
        effective_chunk = chunk_size if chunk_size > 0 else plan.chunk_size

        whole = run_whole_page(
            page.image, detections, self.library, self.params, norm
        )
        chunked, chunk_trace = run_chunked(
            page.image, detections, self.library, self.params, norm,
            effective_chunk,
        )
        assert_byte_identical(page.page_id, whole, chunked)

        # Cache population / reuse, keyed per line; hits must not alter output.
        recompute_lines: list[int] = []
        reused_lines: list[int] = []
        for record in whole:
            key = build_cache_key(
                line_no=record["line_no"],
                image_hash=image_hash,
                boxes_summary=box_hash,
                library_version=self.library.library_version,
                library_checksum=self.library.checksum,
                params_fingerprint=self.params.fingerprint(),
                chunk_size=effective_chunk,
            )
            if use_cache and self.cache.get(key) is not None:
                cached = self.cache.get(key)
                if cached.record != record:
                    # A cache hit that disagrees is an integrity failure, never a
                    # silent override of the freshly computed result.
                    from .errors import InconsistentError
                    raise InconsistentError(
                        "cache hit disagrees with recomputed record",
                        {"page_id": page.page_id, "line_no": record["line_no"]},
                    )
                reused_lines.append(record["line_no"])
            else:
                self.cache.add(
                    CacheEntry(
                        key=key,
                        page_id=page.page_id,
                        line_no=record["line_no"],
                        image_hash=image_hash,
                        boxes_summary=box_hash,
                        library_version=self.library.library_version,
                        library_checksum=self.library.checksum,
                        params_fingerprint=self.params.fingerprint(),
                        chunk_size=effective_chunk,
                        record=record,
                    )
                )
                recompute_lines.append(record["line_no"])

        metrics = compute_metrics(whole, page.truth)
        stats = detection_stats(detections, page.truth)
        result = PageResult(
            page_id=page.page_id,
            image_hash=image_hash,
            boxes=detections,
            norm_stats=norm,
            whole_records=whole,
            chunk_records=chunked,
            chunk_plan={
                "requested_chunk_size": chunk_size,
                "effective_chunk_size": effective_chunk,
                "arbitration": plan.to_json(),
                "chunk_trace": chunk_trace,
            },
            metrics=metrics,
            detection_stats=stats,
        )
        cache_report = {
            "recompute_lines": sorted(recompute_lines),
            "reused_lines": sorted(reused_lines),
            "cache_size": len(self.cache.all_entries()),
        }
        return result, cache_report

    def query_line(
        self,
        page: PageSample,
        line_no: int,
        *,
        latency_budget_ms: float,
        chunk_size: int = 1,
    ) -> dict:
        """Interactive single-line query with real, observed timing."""
        detections = detect_lines(page.image)
        if not 0 <= line_no < len(detections):
            raise InvalidInputError(
                "line number out of range",
                {"page_id": page.page_id, "line_no": line_no,
                 "valid_range": [0, len(detections) - 1]},
            )
        norm = collect_norm_stats(page.image, detections)
        completed_stages = ["detection", "norm_stats"]
        ready_lines: list[int] = []

        started = perf_counter_seconds()
        # Real local recognition loop; no threads, no artificial sleeps.
        subset = list(range(0, min(line_no + chunk_size, len(detections))))
        records = recognize_lines(
            page.image, detections, self.library, self.params, norm,
            line_subset=subset,
        )
        elapsed_ms = (perf_counter_seconds() - started) * 1000.0
        ready_lines = [record["line_no"] for record in records]
        target = next(record for record in records if record["line_no"] == line_no)

        estimate = target["pixel_count"] * target["match_candidates"]
        timing = {
            "elapsed_ms": round6(elapsed_ms),
            "budget_ms": float(latency_budget_ms),
            "estimated_cost_units": estimate,
            "pixel_count": target["pixel_count"],
            "match_candidates": target["match_candidates"],
            "completed_stages": completed_stages + ["recognition"],
            "ready_lines": ready_lines,
            "timing_source": "time.perf_counter around local recognition loop",
        }
        if elapsed_ms > latency_budget_ms:
            raise LatencyBudgetError(
                "single-line query exceeded latency budget",
                {
                    "page_id": page.page_id,
                    "line_no": line_no,
                    "timing": timing,
                },
            )
        return {
            "page_id": page.page_id,
            "line_no": line_no,
            "box": target["box"],
            "text": target["text"],
            "confidence": target["confidence"],
            "candidates": target["candidates"],
            "timing": timing,
        }
