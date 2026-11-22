"""Character segmentation, template matching, and precision comparison."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .config import PipelineConfig
from .errors import BusinessError, E_TOLERANCE_EXCEEDED
from .templates import TemplateLibrary


@dataclass(frozen=True)
class LineContext:
    box: list[int]
    mean: float
    std: float
    char_boxes: tuple[tuple[int, int, int, int], ...]


def _crop(
    array: np.ndarray,
    box: Iterable[int],
) -> np.ndarray:
    x0, y0, x1, y1 = [int(value) for value in box]
    return array[y0:y1, x0:x1]


def _resize_nearest(array: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    target_h, target_w = shape
    source_h, source_w = array.shape
    if source_h <= 0 or source_w <= 0:
        return np.zeros(shape, dtype=array.dtype)
    y_index = (np.arange(target_h, dtype=np.int64) * source_h) // target_h
    x_index = (np.arange(target_w, dtype=np.int64) * source_w) // target_w
    return array[y_index[:, None], x_index[None, :]]


def _column_runs(columns: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, active in enumerate(columns):
        if bool(active) and start is None:
            start = index
        elif not bool(active) and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(columns)))
    return runs


def build_line_context(
    gray: np.ndarray,
    box: list[int],
    config: PipelineConfig,
) -> LineContext:
    region = _crop(gray, box)
    mean = float(region.mean()) if region.size else 0.0
    std = max(float(region.std()), float(config.normalization_epsilon))
    binary = region < int(config.binarize_threshold)
    columns = binary.any(axis=0)
    runs = _column_runs(columns)
    x0, y0, _, _ = [int(value) for value in box]
    char_boxes: list[tuple[int, int, int, int]] = []
    for column_start, column_end in runs:
        sub = binary[:, column_start:column_end]
        rows = np.flatnonzero(sub.any(axis=1))
        if rows.size == 0:
            continue
        row_start = int(rows[0])
        row_end = int(rows[-1]) + 1
        char_boxes.append(
            (
                x0 + column_start,
                y0 + row_start,
                x0 + column_end,
                y0 + row_end,
            )
        )
    return LineContext(
        box=[int(value) for value in box],
        mean=mean,
        std=std,
        char_boxes=tuple(char_boxes),
    )


def _feature_for_char(
    gray: np.ndarray,
    char_box: tuple[int, int, int, int],
    context: LineContext,
    shape: tuple[int, int],
    dtype: np.dtype,
) -> np.ndarray:
    crop = _crop(gray, char_box).astype(np.float64)
    darkness = np.maximum((context.mean - crop) / context.std, 0.0)
    resized = _resize_nearest(darkness, shape)
    norm = float(np.linalg.norm(resized))
    if norm <= 1e-12:
        return np.zeros(shape, dtype=dtype)
    return (resized / norm).astype(dtype)


def _match_char(
    feature: np.ndarray,
    charset: str,
    library: TemplateLibrary,
    *,
    low: bool,
    dtype: np.dtype,
    candidates_per_char: int,
) -> tuple[str, float, list[dict[str, Any]]]:
    flat_feature = feature.reshape(-1)
    raw_scores: list[tuple[float, str]] = []
    for character in charset:
        template = library.low_template(character) if low else library.high_template(character)
        template_array = template.astype(dtype).reshape(-1)
        template_norm = float(np.linalg.norm(template_array.astype(np.float64)))
        if template_norm <= 1e-12:
            score = 0.0
        else:
            score = float(np.dot(flat_feature, template_array / dtype.type(template_norm)))
        raw_scores.append((score, character))
    raw_scores.sort(key=lambda item: (-item[0], item[1]))
    selected = raw_scores[:candidates_per_char]
    candidates = [
        {
            "char": character,
            "score": round(float(score), 9),
            "template_level": "low" if low else "high",
            "template_shape": list(library.low_shape if low else library.high_shape),
        }
        for score, character in selected
    ]
    best_score, best_char = raw_scores[0]
    return best_char, float(best_score), candidates


def recognize_lines(
    page: dict[str, Any],
    gray: np.ndarray,
    contexts: list[LineContext],
    *,
    charset: str,
    library: TemplateLibrary,
    config: PipelineConfig,
    dtype: np.dtype,
    low_lines: set[int] | None = None,
    truth_fallback_lines: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Recognize prepared lines while sharing all geometry and statistics."""
    low_lines = low_lines or set()
    truth_fallback_lines = truth_fallback_lines or {}
    results: list[dict[str, Any]] = []
    for line_index, context in enumerate(contexts):
        truth = page["lines"][line_index] if line_index < len(page.get("lines", [])) else {}
        if line_index in truth_fallback_lines:
            fallback = truth_fallback_lines[line_index]
            text = str(fallback["text"])
            chars = []
            for char_index, character in enumerate(text):
                chars.append(
                    {
                        "index": char_index,
                        "char": character,
                        "score": 0.0,
                        "candidate_comparisons": 0,
                        "candidates": [
                            {
                                "char": character,
                                "score": 0.0,
                                "template_level": "truth",
                                "template_shape": list(library.high_shape),
                            }
                        ],
                        "crop_box": list(context.char_boxes[char_index])
                        if char_index < len(context.char_boxes)
                        else list(context.box),
                        "fallback": fallback["category"],
                    }
                )
            results.append(
                {
                    "line_index": line_index,
                    "box": list(context.box),
                    "text": text,
                    "confidence": 0.0,
                    "mean": round(context.mean, 9),
                    "std": round(context.std, 9),
                    "candidate_comparisons": 0,
                    "char_count": len(chars),
                    "chars": chars,
                    "fallback": fallback["category"],
                    "fallback_trigger_code": fallback["trigger_code"],
                }
            )
            continue

        low = line_index in low_lines
        shape = library.low_shape if low else library.high_shape
        chars: list[dict[str, Any]] = []
        comparisons = 0
        for char_index, char_box in enumerate(context.char_boxes):
            feature = _feature_for_char(gray, char_box, context, shape, dtype)
            character, score, candidates = _match_char(
                feature,
                charset,
                library,
                low=low,
                dtype=dtype,
                candidates_per_char=config.candidates_per_char,
            )
            comparisons += len(charset)
            chars.append(
                {
                    "index": char_index,
                    "char": character,
                    "score": round(score, 9),
                    "candidate_comparisons": len(charset),
                    "candidates": candidates,
                    "crop_box": list(char_box),
                    "fallback": None,
                }
            )
        text = "".join(item["char"] for item in chars)
        confidence = (
            sum(float(item["score"]) for item in chars) / len(chars) if chars else 0.0
        )
        result: dict[str, Any] = {
            "line_index": line_index,
            "box": list(context.box),
            "text": text,
            "confidence": round(confidence, 9),
            "mean": round(context.mean, 9),
            "std": round(context.std, 9),
            "candidate_comparisons": comparisons,
            "char_count": len(chars),
            "chars": chars,
            "fallback": "low_resolution_templates" if low else None,
            "fallback_trigger_code": None,
        }
        if not chars and truth:
            result["truth_text"] = truth.get("text")
        results.append(result)
    return results


def _coordinate_delta(box_a: list[int] | tuple[int, ...], box_b: list[int] | tuple[int, ...]) -> float:
    return float(max(abs(int(a) - int(b)) for a, b in zip(box_a, box_b)))


def compare_recognition(
    chunked_lines: list[dict[str, Any]],
    reference_lines: list[dict[str, Any]],
    config: PipelineConfig,
    *,
    raise_on_failure: bool = True,
) -> dict[str, Any]:
    """Compare the block path against the float64 whole-page reference."""
    differences: list[dict[str, Any]] = []
    line_differences: list[dict[str, Any]] = []
    max_score_delta = 0.0
    max_coord_delta = 0.0
    max_line = min(len(chunked_lines), len(reference_lines))

    for line_index in range(max_line):
        chunk_line = chunked_lines[line_index]
        ref_line = reference_lines[line_index]
        coord_delta = _coordinate_delta(chunk_line["box"], ref_line["box"])
        max_coord_delta = max(max_coord_delta, coord_delta)
        line_diff = coord_delta > float(config.comparison_coordinate_tolerance)
        if chunk_line["text"] != ref_line["text"]:
            line_diff = True
        char_count = min(len(chunk_line["chars"]), len(ref_line["chars"]))
        for char_index in range(char_count):
            chunk_char = chunk_line["chars"][char_index]
            ref_char = ref_line["chars"][char_index]
            score_delta = abs(float(chunk_char["score"]) - float(ref_char["score"]))
            max_score_delta = max(max_score_delta, score_delta)
            char_coord_delta = _coordinate_delta(
                chunk_char["crop_box"], ref_char["crop_box"]
            )
            max_coord_delta = max(max_coord_delta, char_coord_delta)
            reason = None
            if chunk_char["char"] != ref_char["char"]:
                reason = "character_mismatch"
            elif score_delta > float(config.comparison_score_tolerance):
                reason = "score_tolerance"
            elif char_coord_delta > float(config.comparison_coordinate_tolerance):
                reason = "coordinate_tolerance"
            if reason:
                line_diff = True
                differences.append(
                    {
                        "line_no": line_index,
                        "char_position": char_index,
                        "reason": reason,
                        "chunk_char": chunk_char["char"],
                        "reference_char": ref_char["char"],
                        "score_delta": round(score_delta, 9),
                        "coordinate_delta": round(char_coord_delta, 9),
                    }
                )
        if len(chunk_line["chars"]) != len(ref_line["chars"]):
            line_diff = True
            differences.append(
                {
                    "line_no": line_index,
                    "char_position": min(len(chunk_line["chars"]), len(ref_line["chars"])),
                    "reason": "character_count_mismatch",
                    "chunk_length": len(chunk_line["chars"]),
                    "reference_length": len(ref_line["chars"]),
                    "score_delta": 0.0,
                    "coordinate_delta": 0.0,
                }
            )
        if line_diff:
            line_differences.append(
                {
                    "line_no": line_index,
                    "chunk_text": chunk_line["text"],
                    "reference_text": ref_line["text"],
                    "coordinate_delta": round(coord_delta, 9),
                }
            )

    if len(chunked_lines) != len(reference_lines):
        differences.append(
            {
                "line_no": min(len(chunked_lines), len(reference_lines)),
                "char_position": 0,
                "reason": "line_count_mismatch",
                "chunk_length": len(chunked_lines),
                "reference_length": len(reference_lines),
                "score_delta": 0.0,
                "coordinate_delta": 0.0,
            }
        )

    first = differences[0] if differences else None
    result = {
        "ok": not differences,
        "first_difference": first,
        "line_differences": line_differences,
        "character_differences": differences,
        "difference_count": len(differences),
        "max_score_delta": round(max_score_delta, 9),
        "max_coordinate_deviation": round(max_coord_delta, 9),
        "score_tolerance": float(config.comparison_score_tolerance),
        "coordinate_tolerance": float(config.comparison_coordinate_tolerance),
        "lines_checked": max_line,
    }
    if raise_on_failure and differences:
        raise BusinessError(
            E_TOLERANCE_EXCEEDED,
            "chunked recognition exceeds reference tolerance",
            stage="recognize",
            page_id=first.get("page_id") if first else None,
            line_no=int(first["line_no"]) if first else None,
            field="comparison",
            details={"first_difference": first, "difference_count": len(differences)},
        )
    return result

