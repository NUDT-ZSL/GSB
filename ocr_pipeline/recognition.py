"""Stage 2: deterministic per-line recognition and stage 3 aggregation.

Recognition always normalizes against *page-global* ink statistics collected up
front, so a line recognized inside a chunk uses exactly the same statistics as
one recognized in a single whole-page pass.  Matching, tie-breaking, confidence
quantization and line de-duplication are pure functions of the pixels, the
template library and the recognition parameters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .detection import DetectedLine, _iou
from .templates import GLYPH_WIDTH, TemplateLibrary
from .util import content_hash, round6


@dataclass(frozen=True)
class RecognitionParams:
    dedup_iou: float = 0.8
    confidence_decimals: int = 6
    score_match_weight: float = 0.5
    score_correlation_weight: float = 0.5

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict) -> "RecognitionParams":
        return cls(**payload)

    def fingerprint(self) -> str:
        return content_hash(self.to_json())


@dataclass(frozen=True)
class NormStats:
    mean: float
    std: float
    ink_pixel_count: int
    digest: str

    def to_json(self) -> dict:
        return {
            "mean": round6(self.mean),
            "std": round6(self.std),
            "ink_pixel_count": self.ink_pixel_count,
            "digest": self.digest,
        }


def collect_norm_stats(
    image: np.ndarray, detections: list[DetectedLine]
) -> NormStats:
    """Page-global normalization statistics, identical for every chunking plan."""
    # Normalization is defined in binary glyph space so rendered ink and stroke
    # templates share one scale.  Statistics are pooled over every detected line
    # pixel up front, making them independent of chunk boundaries.
    binary_pixels: list[np.ndarray] = []
    ink_count = 0
    for line in detections:
        x1, y1, x2, y2 = line.box
        crop = image[y1:y2 + 1, x1:x2 + 1]
        binary = (crop < 128).astype(np.float64)
        binary_pixels.append(binary.reshape(-1))
        ink_count += int(binary.sum())
    values = (
        np.concatenate(binary_pixels) if binary_pixels
        else np.zeros(0, dtype=np.float64)
    )
    mean = float(values.mean()) if values.size else 0.0
    std = float(values.std()) if values.size else 1.0
    if std == 0.0:
        std = 1.0
    digest = content_hash(
        {
            "mean": round6(mean),
            "std": round6(std),
            "ink_pixel_count": int(ink_count),
        }
    )
    return NormStats(mean=mean, std=std, ink_pixel_count=int(ink_count), digest=digest)


def _glyph_slots(crop: np.ndarray) -> list[tuple[int, int]]:
    """Split a line crop into fixed-width glyph slots based on dark columns."""
    column_dark = (crop < 128).sum(axis=0)
    occupied = column_dark > 0
    slots: list[tuple[int, int]] = []
    start: int | None = None
    for x, is_dark in enumerate(occupied):
        if is_dark and start is None:
            start = x
        elif not is_dark and start is not None:
            slots.append((start, x - 1))
            start = None
    if start is not None:
        slots.append((start, len(occupied) - 1))
    # Each rendered glyph is GLYPH_WIDTH wide with a 2-column gap; collapse any
    # accidental splits back to the known slot width deterministically.
    normalized: list[tuple[int, int]] = []
    for slot_start, _ in slots:
        end = min(slot_start + GLYPH_WIDTH - 1, crop.shape[1] - 1)
        if not normalized or slot_start > normalized[-1][1]:
            normalized.append((slot_start, end))
    return normalized


def _score_glyph(
    glyph: np.ndarray, template: np.ndarray, norm: NormStats, params: RecognitionParams
) -> float:
    glyph_binary = (glyph < 128).astype(np.float64)
    template_binary = template.astype(np.float64)
    # Shared page-global standardization (same mean/std for both operands).
    glyph_norm = (glyph_binary - norm.mean) / norm.std
    template_norm = (template_binary - norm.mean) / norm.std
    denom = np.sqrt((glyph_norm ** 2).sum() * (template_norm ** 2).sum())
    correlation = (
        float((glyph_norm * template_norm).sum() / denom) if denom > 0 else 0.0
    )
    agreement = float((glyph_binary == template_binary).mean())
    score = (
        params.score_correlation_weight * correlation
        + params.score_match_weight * agreement
    )
    return round6(score)


def recognize_line(
    image: np.ndarray,
    detection: DetectedLine,
    library: TemplateLibrary,
    params: RecognitionParams,
    norm: NormStats,
) -> dict:
    x1, y1, x2, y2 = detection.box
    crop = image[y1:y2 + 1, x1:x2 + 1]
    slots = _glyph_slots(crop)
    chars: list[str] = []
    confidences: list[float] = []
    candidates_per_slot: list[list[dict]] = []

    for slot_x1, slot_x2 in slots:
        glyph = crop[:, slot_x1:slot_x2 + 1]
        if glyph.shape[1] != GLYPH_WIDTH:
            # Right-pad deterministically if the final slot clips the border.
            pad = np.full(
                (glyph.shape[0], GLYPH_WIDTH - glyph.shape[1]), 255, dtype=np.uint8
            )
            glyph = np.concatenate([glyph, pad], axis=1)
        scored = [
            {
                "char": char,
                "score": _score_glyph(glyph, library.templates[index], norm, params),
            }
            for index, char in enumerate(library.alphabet)
        ]
        # Deterministic order: highest score, then lowest alphabet index.
        scored.sort(key=lambda item: (-item["score"], item["char"]))
        best = scored[0]
        chars.append(best["char"])
        confidences.append(best["score"])
        candidates_per_slot.append(scored)

    line_confidence = round6(
        float(np.mean(confidences)) if confidences else 0.0
    )
    pixel_count = int(crop.size)
    match_candidates = len(slots) * library.glyph_count
    return {
        "line_no": detection.line_no,
        "box": list(detection.box),
        "text": "".join(chars),
        "confidence": round(line_confidence, params.confidence_decimals),
        "slot_count": len(slots),
        "pixel_count": pixel_count,
        "match_candidates": match_candidates,
        "estimated_cost": pixel_count * match_candidates,
        "norm_digest": norm.digest,
        "candidates": candidates_per_slot,
    }


def recognize_lines(
    image: np.ndarray,
    detections: list[DetectedLine],
    library: TemplateLibrary,
    params: RecognitionParams,
    norm: NormStats,
    line_subset: list[int] | None = None,
) -> list[dict]:
    selected = (
        detections
        if line_subset is None
        else [detections[index] for index in line_subset]
    )
    return [
        recognize_line(image, detection, library, params, norm)
        for detection in selected
    ]


def deduplicate_lines(
    line_records: list[dict], params: RecognitionParams
) -> list[dict]:
    """Deterministic IoU de-duplication; keeps the highest-confidence box."""
    ordered = sorted(
        line_records, key=lambda record: (record["box"][1], record["box"][0])
    )
    kept: list[dict] = []
    for record in ordered:
        duplicate_of: int | None = None
        for existing in kept:
            if _iou(tuple(record["box"]), tuple(existing["box"])) >= params.dedup_iou:
                duplicate_of = existing["line_no"]
                break
        if duplicate_of is None:
            kept.append(record)
        else:
            # Deterministic replacement rule, applied identically at chunk joins.
            incumbent = next(item for item in kept if item["line_no"] == duplicate_of)
            if (record["confidence"], -record["line_no"]) > (
                incumbent["confidence"],
                -incumbent["line_no"],
            ):
                kept[kept.index(incumbent)] = record
    kept.sort(key=lambda record: (record["box"][1], record["box"][0]))
    return kept
