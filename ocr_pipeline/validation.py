"""Independent cross-module consistency validation.

The validator recomputes facts from raw inputs rather than trusting pipeline
bookkeeping.  Every failure is raised as ``E_INCONSISTENT`` with a category and
the offending page_id.
"""

from __future__ import annotations

from .cache import RecognitionCache, build_cache_key
from .chunking import run_chunked, run_whole_page
from .detection import _iou, boxes_summary, detect_lines, detection_stats
from .errors import InconsistentError
from .metrics import compute_metrics, recompute_from_per_line
from .recognition import RecognitionParams, collect_norm_stats
from .templates import TemplateLibrary
from .util import array_hash, canonical_bytes, content_hash


def _fail(category: str, page_id: str, detail: dict | None = None) -> None:
    context = {"category": category, "page_id": page_id}
    if detail:
        context.update(detail)
    raise InconsistentError(f"consistency check failed: {category}", context)


def validate_boxes_no_overlap(page_id: str, boxes: list[tuple[int, ...]]) -> None:
    ordered = sorted(boxes, key=lambda box: (box[1], box[0]))
    for index, box in enumerate(ordered):
        x1, y1, x2, y2 = box
        if not (0 <= x1 <= x2 and 0 <= y1 <= y2):
            _fail("box_out_of_order", page_id, {"box": list(box)})
        for other in ordered[index + 1:]:
            # Lines may not overlap beyond the de-dup IoU boundary.
            if _iou(box, other) >= 0.8:
                _fail("boxes_overlap", page_id,
                      {"box_a": list(box), "box_b": list(other)})


def validate_truth_coverage(page_id: str, page, detections) -> None:
    stats = detection_stats(detections, page.truth)
    if stats["covered_truth_lines"] != stats["truth_lines"]:
        _fail("truth_coverage_incomplete", page_id, stats)


def validate_records(page_id: str, records: list[dict], alphabet: str) -> None:
    for record in records:
        if len(record["text"]) != record["slot_count"]:
            _fail("text_length_mismatch", page_id,
                  {"line_no": record["line_no"]})
        if len(record["candidates"]) != record["slot_count"]:
            _fail("candidate_count_mismatch", page_id,
                  {"line_no": record["line_no"]})
        for slot in record["candidates"]:
            if len(slot) != len(alphabet):
                _fail("candidate_alphabet_mismatch", page_id,
                      {"line_no": record["line_no"]})
            for choice in slot:
                if choice["char"] not in alphabet:
                    _fail("candidate_char_unknown", page_id,
                          {"line_no": record["line_no"], "char": choice["char"]})


def validate_metrics_recompute(page_id: str, page, records: list[dict]) -> None:
    metrics = compute_metrics(records, page.truth)
    rebuilt = recompute_from_per_line(metrics["per_line"])
    for key in ("line_accuracy", "char_accuracy", "correct_lines",
                "total_lines", "correct_chars", "total_chars"):
        if rebuilt[key] != metrics[key]:
            _fail("metrics_recompute_mismatch", page_id,
                  {"key": key, "metrics": metrics[key], "rebuilt": rebuilt[key]})


def validate_cache_coherence(
    page_id: str, page, detections, library: TemplateLibrary,
    params: RecognitionParams, chunk_size: int, records: list[dict],
    cache: RecognitionCache,
) -> None:
    image_hash = array_hash(page.image)
    box_hash = boxes_summary(detections)
    for record in records:
        key = build_cache_key(
            line_no=record["line_no"],
            image_hash=image_hash,
            boxes_summary=box_hash,
            library_version=library.library_version,
            library_checksum=library.checksum,
            params_fingerprint=params.fingerprint(),
            chunk_size=chunk_size,
        )
        entry = cache.get(key)
        if entry is None:
            _fail("cache_entry_missing", page_id, {"line_no": record["line_no"]})
        elif canonical_bytes(entry.record) != canonical_bytes(record):
            _fail("cache_record_mismatch", page_id, {"line_no": record["line_no"]})


def validate_invalidation_sets(
    page_id: str, affected: list[int], recompute: list[int], reused: list[int]
) -> None:
    recompute_set, reused_set = set(recompute), set(reused)
    if recompute_set & reused_set:
        _fail("recompute_reuse_overlap", page_id,
              {"intersection": sorted(recompute_set & reused_set)})
    if recompute_set | reused_set != set(affected):
        _fail("recompute_reuse_union", page_id,
              {"affected": sorted(affected),
               "union": sorted(recompute_set | reused_set)})


def validate_query_vs_page(
    page_id: str, query_result: dict, page_records: list[dict],
    precision: float = 1e-6,
) -> None:
    record = next(
        (item for item in page_records if item["line_no"] == query_result["line_no"]),
        None,
    )
    if record is None:
        _fail("query_line_missing_in_page", page_id,
              {"line_no": query_result["line_no"]})
    if query_result["text"] != record["text"] or query_result["box"] != record["box"]:
        _fail("query_page_result_mismatch", page_id,
              {"line_no": record["line_no"]})
    if abs(query_result["confidence"] - record["confidence"]) > precision:
        _fail("query_confidence_precision", page_id,
              {"line_no": record["line_no"],
               "query": query_result["confidence"],
               "page": record["confidence"]})


def validate_full_page(
    page, library: TemplateLibrary, params: RecognitionParams,
    chunk_size: int, cache: RecognitionCache,
) -> dict:
    """Run every independent check for one page and return a summary."""
    detections = detect_lines(page.image)
    norm = collect_norm_stats(page.image, detections)
    whole = run_whole_page(page.image, detections, library, params, norm)
    chunked, _trace = run_chunked(
        page.image, detections, library, params, norm, chunk_size
    )
    if canonical_bytes(whole) != canonical_bytes(chunked):
        _fail("chunk_byte_mismatch", page.page_id)

    validate_boxes_no_overlap(
        page.page_id, [detection.box for detection in detections]
    )
    validate_truth_coverage(page.page_id, page, detections)
    validate_records(page.page_id, whole, library.alphabet)
    validate_metrics_recompute(page.page_id, page, whole)
    validate_cache_coherence(
        page.page_id, page, detections, library, params, chunk_size, whole, cache
    )
    return {
        "page_id": page.page_id,
        "checks_passed": [
            "chunk_byte_identity",
            "boxes_no_overlap",
            "truth_coverage",
            "record_candidates",
            "metrics_recompute",
            "cache_coherence",
        ],
        "result_hash": content_hash(whole),
    }
