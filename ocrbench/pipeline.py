"""Pure deterministic computation for the six OCR graph nodes.

These functions contain no caching, I/O or time-dependent state.  Given the
same arrays, template library and config they always return byte-identical
plain-data structures, which makes them safe to hash, cache and compare.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .config import PipelineConfig
from .deterministic import content_hash, quantize
from .errors import ErrorCode, make_error
from .templates import GLYPH_H, GLYPH_W, TemplateLibrary


# ---------------------------------------------------------------- preprocess
def preprocess(image: np.ndarray, config: PipelineConfig) -> Dict[str, Any]:
    """Binarize the grayscale page using the configured intensity threshold."""

    mask = (image < config.detect_threshold).astype(np.uint8)
    return {
        "shape": list(image.shape),
        "detect_threshold": config.detect_threshold,
        "mask": mask.tolist(),
    }


# -------------------------------------------------------------------- detect
def detect_lines(
    preprocessed: Dict[str, Any], page_id: str
) -> Dict[str, Any]:
    """Detect text line bands and their x-extents from the ink mask."""

    mask = np.asarray(preprocessed["mask"], dtype=np.uint8)
    row_ink = mask.sum(axis=1)
    active = row_ink > 0
    boxes: List[List[int]] = []
    row = 0
    height = mask.shape[0]
    while row < height:
        if not active[row]:
            row += 1
            continue
        y0 = row
        while row < height and active[row]:
            row += 1
        y1 = row
        band = mask[y0:y1, :]
        col_ink = band.sum(axis=0)
        ink_cols = np.flatnonzero(col_ink > 0)
        if ink_cols.size == 0:
            continue
        x0 = int(ink_cols[0])
        x1 = int(ink_cols[-1]) + 1
        boxes.append([x0, y0, x1, y1])

    if not boxes:
        raise make_error(
            ErrorCode.DETECT_EMPTY,
            "text line detection produced no lines",
            page_id=page_id,
            stage="detect",
        )
    return {"boxes": boxes, "line_box_summary": line_box_summary(boxes)}


def line_box_summary(boxes: Sequence[Sequence[int]]) -> str:
    return content_hash([list(box) for box in boxes])[:16]


# ------------------------------------------------------------------ features
def _segment_cells(box: Sequence[int], mask: np.ndarray) -> List[np.ndarray]:
    x0, y0, x1, y1 = (int(v) for v in box)
    width = x1 - x0
    cell_count = (width + 1) // (GLYPH_W + 1)
    cells: List[np.ndarray] = []
    for index in range(cell_count):
        cx0 = x0 + index * (GLYPH_W + 1)
        cx1 = cx0 + GLYPH_W
        if cx1 > mask.shape[1]:
            break
        cells.append(mask[y0:y0 + GLYPH_H, cx0:cx1].astype(np.float64))
    return cells


def extract_features(
    preprocessed: Dict[str, Any], detected: Dict[str, Any], config: PipelineConfig
) -> Dict[str, Any]:
    """Segment character cells and apply page-level normalized features.

    Normalization statistics are computed once over every cell on the page so
    chunk boundaries can never change the statistics.
    """

    mask = np.asarray(preprocessed["mask"], dtype=np.uint8)
    boxes = detected["boxes"]
    raw_cells: List[List[np.ndarray]] = [
        _segment_cells(box, mask) for box in boxes
    ]
    flat = [cell for line_cells in raw_cells for cell in line_cells]
    if not flat:
        raise make_error(
            ErrorCode.MATCH_FAILED,
            "no character cells could be segmented",
            stage="features",
        )
    stacked = np.stack([cell.reshape(-1) for cell in flat])
    global_mean = float(stacked.mean())
    global_std = float(stacked.std())
    scale = config.confidence_scale

    lines: List[Dict[str, Any]] = []
    for line_index, cells in enumerate(raw_cells):
        normalized_cells: List[List[int]] = []
        for cell in cells:
            vector = cell.reshape(-1).astype(np.float64)
            if global_std > 0:
                normalized = (vector - global_mean) / global_std
            else:
                normalized = vector - global_mean
            normalized_cells.append([quantize(value, scale) for value in normalized])
        lines.append(
            {
                "line_index": line_index,
                "cell_count": len(normalized_cells),
                "features": normalized_cells,
            }
        )
    return {
        "lines": lines,
        "norm_mean": quantize(global_mean, scale),
        "norm_std": quantize(global_std, scale),
        "line_box_summary": detected["line_box_summary"],
    }


def template_feature_bank(
    library: TemplateLibrary, global_mean: float, global_std: float, config: PipelineConfig
) -> Dict[str, List[int]]:
    """Normalize every template glyph with the same page-level statistics."""

    bank: Dict[str, List[int]] = {}
    scale = config.confidence_scale
    for char in library.charset:
        vector = (library.glyph(char) > 0).astype(np.float64).reshape(-1)
        if global_std > 0:
            normalized = (vector - global_mean) / global_std
        else:
            normalized = vector - global_mean
        bank[char] = [quantize(value, scale) for value in normalized]
    return bank


# --------------------------------------------------------------------- match
def match_lines(
    features: Dict[str, Any],
    library: TemplateLibrary,
    config: PipelineConfig,
    page_id: str,
) -> Dict[str, Any]:
    """Score every cell against the full charset using quantized SAD."""

    mean = features["norm_mean"] / config.confidence_scale
    std = features["norm_std"] / config.confidence_scale
    bank = template_feature_bank(library, mean, std, config)
    charset = list(library.charset)
    candidate_count = len(charset)

    lines: List[Dict[str, Any]] = []
    for line in features["lines"]:
        line_index = int(line["line_index"])
        if candidate_count > config.resource_limits.max_match_candidates:
            from .resource import check_match_candidates

            check_match_candidates(
                candidate_count, config.resource_limits, page_id, line_index
            )
        cell_results: List[Dict[str, Any]] = []
        if line["cell_count"] == 0:
            raise make_error(
                ErrorCode.MATCH_FAILED,
                "line has no segmented cells to match",
                page_id=page_id,
                stage="match",
                line_index=line_index,
            )
        for cell_index, feature in enumerate(line["features"]):
            scored: List[Tuple[int, str]] = []
            for char in charset:
                template = bank[char]
                score = int(sum(abs(a - b) for a, b in zip(feature, template)))
                scored.append((score, char))
            scored.sort(key=lambda item: (item[0], item[1]))
            best_score, best_char = scored[0]
            second_score = scored[1][0] if len(scored) > 1 else best_score
            margin = second_score - best_score
            confidence = quantize(margin / max(candidate_count, 1), config.confidence_scale)
            cell_results.append(
                {
                    "cell_index": cell_index,
                    "char": best_char,
                    "score": best_score,
                    "confidence": confidence,
                    "top_candidates": [
                        {"char": char, "score": score}
                        for score, char in scored[:3]
                    ],
                }
            )
        lines.append({"line_index": line_index, "cells": cell_results})
    return {
        "lines": lines,
        "charset_version": library.version,
        "line_box_summary": features["line_box_summary"],
    }


# ----------------------------------------------------------------- recognize
def recognize_lines(
    matched: Dict[str, Any], config: PipelineConfig
) -> Dict[str, Any]:
    """Assemble per-line strings and apply page-global inter-line dedup."""

    records: List[Dict[str, Any]] = []
    for line in matched["lines"]:
        text = "".join(cell["char"] for cell in line["cells"])
        if not text:
            raise make_error(
                ErrorCode.MATCH_FAILED,
                "recognition produced an empty line",
                stage="recognize",
                line_index=int(line["line_index"]),
            )
        confidence_values = [cell["confidence"] for cell in line["cells"]]
        mean_confidence = (
            sum(confidence_values) // len(confidence_values)
            if confidence_values
            else 0
        )
        records.append(
            {
                "line_index": int(line["line_index"]),
                "text": text,
                "confidence": mean_confidence,
            }
        )

    _apply_dedup(records, config)
    return {
        "lines": records,
        "charset_version": matched["charset_version"],
        "dedup_threshold": quantize(config.dedup_threshold, config.confidence_scale),
        "line_box_summary": matched["line_box_summary"],
    }


def _line_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    length = max(len(a), len(b))
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / length


def _apply_dedup(records: List[Dict[str, Any]], config: PipelineConfig) -> None:
    """Assign deterministic dedup groups using the configured threshold.

    A pair belongs to the same group when similarity exceeds
    ``1 - dedup_threshold``.  Groups are assigned in line order so the result
    is stable and identical for whole-page and chunked execution.
    """

    similarity_cutoff = 1.0 - config.dedup_threshold
    group_of: List[int] = []
    for index, record in enumerate(records):
        assigned = None
        for earlier in range(index):
            if (
                _line_similarity(record["text"], records[earlier]["text"])
                >= similarity_cutoff
            ):
                assigned = group_of[earlier]
                break
        if assigned is None:
            assigned = index
        group_of.append(assigned)
    for record, group in zip(records, group_of):
        record["dedup_group"] = group


# ----------------------------------------------------------------- aggregate
def aggregate_page(
    recognized: Dict[str, Any],
    truth_lines: Sequence[Any],
    config: PipelineConfig,
    page_id: str,
) -> Dict[str, Any]:
    """Compute char/line accuracy with per-line independently recheckable detail."""

    recognized_lines = recognized["lines"]
    per_line: List[Dict[str, Any]] = []
    total_truth_chars = 0
    total_correct_chars = 0
    exact_lines = 0
    for offset, truth in enumerate(truth_lines):
        truth_text = truth.text if hasattr(truth, "text") else str(truth["text"])
        if offset < len(recognized_lines):
            pred_text = recognized_lines[offset]["text"]
        else:
            pred_text = ""
        correct_chars = sum(
            1 for a, b in zip(truth_text, pred_text) if a == b
        )
        line_exact = int(pred_text == truth_text and len(pred_text) == len(truth_text))
        exact_lines += line_exact
        total_truth_chars += len(truth_text)
        total_correct_chars += correct_chars
        per_line.append(
            {
                "line_index": offset,
                "truth": truth_text,
                "prediction": pred_text,
                "correct_chars": correct_chars,
                "truth_chars": len(truth_text),
                "line_exact": bool(line_exact),
            }
        )

    line_count = max(len(truth_lines), 1)
    char_accuracy = (
        total_correct_chars / total_truth_chars if total_truth_chars else 0.0
    )
    line_accuracy = exact_lines / line_count
    payload = {
        "page_id": page_id,
        "char_accuracy": quantize(char_accuracy, config.confidence_scale),
        "line_accuracy": quantize(line_accuracy, config.confidence_scale),
        "exact_lines": exact_lines,
        "total_lines": len(truth_lines),
        "correct_chars": total_correct_chars,
        "total_truth_chars": total_truth_chars,
        "per_line": per_line,
        "line_box_summary": recognized["line_box_summary"],
        "charset_version": recognized["charset_version"],
    }
    payload["metrics_hash"] = content_hash(
        [payload["char_accuracy"], payload["line_accuracy"], per_line]
    )
    return payload
