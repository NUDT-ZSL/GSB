"""Cross-format panoptic fusion, deterministic tie breaks and boundary probes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import E_BOUNDARY_SENSITIVE, PipelineError
from .models import V1, V2, InstanceRecord
from .rle import bitmap_to_runs, runs_to_bitmap


@dataclass
class FusedInstance:
    fused_id: str
    scene_id: str
    semantic_class: str
    width: int
    height: int
    runs: tuple[tuple[int, int], ...]
    pixel_count: int
    confidence: float
    is_crowd: bool
    contributors: list[str]
    source_versions: list[str]
    match_iou: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fused_id": self.fused_id,
            "scene_id": self.scene_id,
            "semantic_class": self.semantic_class,
            "pixel_count": self.pixel_count,
            "confidence": round(self.confidence, 12),
            "is_crowd": self.is_crowd,
            "contributors": list(self.contributors),
            "source_versions": list(self.source_versions),
            "match_iou": None if self.match_iou is None else round(self.match_iou, 15),
            "runs": [[start, length] for start, length in self.runs],
        }


@dataclass
class ConflictRecord:
    scene_id: str
    kind: str
    winning_instance: str
    losing_instance: str
    semantic_class: str
    iou: float | None
    confidence_winner: float
    confidence_loser: float
    sort_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene_id,
            "kind": self.kind,
            "winning_instance": self.winning_instance,
            "losing_instance": self.losing_instance,
            "semantic_class": self.semantic_class,
            "iou": None if self.iou is None else round(self.iou, 15),
            "confidence_winner": round(self.confidence_winner, 12),
            "confidence_loser": round(self.confidence_loser, 12),
            "sort_key": self.sort_key,
        }


@dataclass
class BoundaryProbe:
    instance_id: str
    scene_id: str
    kind: str
    reference_value: float
    actual_value: float
    difference: float
    reference_decision: bool
    actual_decision: bool
    sensitive: bool
    sort_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "scene_id": self.scene_id,
            "kind": self.kind,
            "reference_value": repr(self.reference_value),
            "actual_value": repr(self.actual_value),
            "difference": repr(self.difference),
            "reference_decision": self.reference_decision,
            "actual_decision": self.actual_decision,
            "sensitive": self.sensitive,
            "code": E_BOUNDARY_SENSITIVE if self.sensitive else None,
            "sort_key": self.sort_key,
        }


@dataclass
class FusionResult:
    scene_id: str
    width: int
    height: int
    fused_instances: list[FusedInstance] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    boundary_probes: list[BoundaryProbe] = field(default_factory=list)
    label_map: np.ndarray | None = None
    dropped_instances: list[str] = field(default_factory=list)


def _iou_pairs(a: np.ndarray, b: np.ndarray) -> tuple[float, int, int]:
    intersection = int(np.count_nonzero(a & b))
    union = int(np.count_nonzero(a | b))
    if union == 0:
        return 0.0, intersection, union
    return intersection / union, intersection, union


def _reference_iou(intersection: int, union: int) -> float:
    """Exact double precision reference using Python ints before division."""
    if union == 0:
        return 0.0
    return float(intersection) / float(union)


def _actual_iou(a: np.ndarray, b: np.ndarray) -> float:
    """Reduced precision (float32) decision path probed by boundary diagnostics."""
    intersection = np.float32(np.count_nonzero(a & b))
    union = np.float32(np.count_nonzero(a | b))
    if float(union) == 0.0:
        return 0.0
    return float(np.float32(intersection / union))


def _match_key(iou: float, confidence: float, instance_id: str) -> tuple:
    return (-iou, -confidence, instance_id)


def _confidence_weighted_merge(
    record_a: InstanceRecord,
    record_b: InstanceRecord,
    bitmap_a: np.ndarray,
    bitmap_b: np.ndarray,
) -> np.ndarray:
    """Confidence weighted merge with an explicit deterministic tie rule.

    Pixels claimed by only one side keep that side.  Pixels claimed by both
    sides go to the higher confidence; on an exact confidence tie the smaller
    instance id wins lexicographically.
    """

    a = bitmap_a.astype(bool)
    b = bitmap_b.astype(bool)
    if record_a.confidence > record_b.confidence:
        a_wins = True
    elif record_b.confidence > record_a.confidence:
        a_wins = False
    else:
        a_wins = record_a.instance_id < record_b.instance_id
    if a_wins:
        winner_mask, loser_mask = a, b
    else:
        winner_mask, loser_mask = b, a
    merged = winner_mask.copy()
    # Pixels only the loser covers still belong to the fused instance.
    merged |= loser_mask & ~winner_mask
    return merged.astype(np.uint8)


def fuse_scene(
    scene_id: str,
    records: list[InstanceRecord],
    iou_threshold: float,
    min_coverage: float,
    retention_policy: str,
    boundary_iou_threshold: float | None = None,
) -> FusionResult:
    if not records:
        raise PipelineError("E_INPUT_INVALID", f"no records for scene {scene_id}")
    width = records[0].width
    height = records[0].height
    total_pixels = width * height
    ordered = sorted(records, key=lambda item: item.instance_id)

    bitmaps: dict[str, np.ndarray] = {}
    for record in ordered:
        bitmaps[record.instance_id] = runs_to_bitmap(record.runs, height, width)

    v1_records = [r for r in ordered if r.source_version == V1 and not r.is_crowd]
    v2_records = [r for r in ordered if r.source_version == V2 and not r.is_crowd]
    crowd_records = [r for r in ordered if r.is_crowd]

    result = FusionResult(scene_id=scene_id, width=width, height=height)

    candidate_pairs: list[tuple[float, str, str, InstanceRecord, InstanceRecord]] = []
    for record_v1 in v1_records:
        for record_v2 in v2_records:
            if record_v1.semantic_class != record_v2.semantic_class:
                continue
            iou, _intersection, _union = _iou_pairs(bitmaps[record_v1.instance_id], bitmaps[record_v2.instance_id])
            if iou >= iou_threshold:
                candidate_pairs.append(
                    (iou, record_v1.instance_id, record_v2.instance_id, record_v1, record_v2)
                )

    probe_threshold = iou_threshold if boundary_iou_threshold is None else boundary_iou_threshold
    for record_v1 in v1_records:
        for record_v2 in v2_records:
            if record_v1.semantic_class != record_v2.semantic_class:
                continue
            _iou, intersection, union = _iou_pairs(
                bitmaps[record_v1.instance_id], bitmaps[record_v2.instance_id]
            )
            result.boundary_probes.append(
                iou_boundary_probe(
                    scene_id,
                    f"{record_v1.instance_id}|{record_v2.instance_id}",
                    bitmaps[record_v1.instance_id],
                    bitmaps[record_v2.instance_id],
                    intersection,
                    union,
                    probe_threshold,
                )
            )

    candidate_pairs.sort(
        key=lambda item: _match_key(
            item[0],
            max(item[3].confidence, item[4].confidence),
            min(item[1], item[2]),
        )
    )

    matched: set[str] = set()
    merged_specs: list[tuple[InstanceRecord, InstanceRecord, np.ndarray, float]] = []
    for iou, id_v1, id_v2, record_v1, record_v2 in candidate_pairs:
        if id_v1 in matched or id_v2 in matched:
            winner = id_v1 if id_v1 not in matched else id_v2
            loser = id_v2 if winner == id_v1 else id_v1
            if loser not in matched:
                result.conflicts.append(
                    ConflictRecord(
                        scene_id=scene_id,
                        kind="match_contest",
                        winning_instance=winner,
                        losing_instance=loser,
                        semantic_class=record_v1.semantic_class,
                        iou=iou,
                        confidence_winner=next(r.confidence for r in (record_v1, record_v2) if r.instance_id == winner),
                        confidence_loser=next(r.confidence for r in (record_v1, record_v2) if r.instance_id == loser),
                        sort_key=f"{scene_id}:match_contest:{loser}:{winner}",
                    )
                )
            continue
        matched.add(id_v1)
        matched.add(id_v2)
        merged_bitmap = _confidence_weighted_merge(
            record_v1, record_v2, bitmaps[id_v1], bitmaps[id_v2]
        )
        merged_specs.append((record_v1, record_v2, merged_bitmap, iou))

    standalone = [r for r in ordered if r.instance_id not in matched]

    fused_specs: list[tuple[str, str, bool, float, list[str], list[str], np.ndarray, float | None]] = []
    for record_v1, record_v2, merged_bitmap, iou in merged_specs:
        fused_specs.append(
            (
                record_v1.semantic_class,
                f"{record_v1.instance_id}|{record_v2.instance_id}",
                False,
                (record_v1.confidence + record_v2.confidence) / 2.0,
                [record_v1.instance_id, record_v2.instance_id],
                [V1, V2],
                merged_bitmap,
                iou,
            )
        )
    for record in standalone:
        fused_specs.append(
            (
                record.semantic_class,
                record.instance_id,
                record.is_crowd,
                record.confidence,
                [record.instance_id],
                [record.source_version],
                bitmaps[record.instance_id],
                None,
            )
        )

    fused_specs.sort(key=lambda spec: spec[1])

    # Retention policy: coverage is evaluated before ownership assignment so
    # the decision is based on the instance's own mask content.
    retained: list[tuple] = []
    for spec in fused_specs:
        semantic_class, fused_name, is_crowd, confidence, contributors, versions, bitmap, match_iou = spec
        coverage = int(np.count_nonzero(bitmap)) / float(total_pixels)
        keep = True
        if retention_policy == "drop_below_coverage" and coverage < min_coverage:
            keep = False
            result.dropped_instances.append(fused_name)
        if keep:
            retained.append(spec)
        result.boundary_probes.append(
            _coverage_probe(scene_id, fused_name, int(np.count_nonzero(bitmap)), total_pixels, min_coverage)
        )

    # Unique pixel ownership: non-crowd wins over crowd, then confidence desc,
    # then fused name lexicographic. Conflicts are recorded deterministically.
    owner = np.full(total_pixels, -1, dtype=np.int32)
    order = sorted(
        range(len(retained)),
        key=lambda idx: (bool(retained[idx][2]), -retained[idx][3], retained[idx][1]),
    )
    for idx in order:
        spec = retained[idx]
        bitmap = spec[6].reshape(-1)
        active = np.flatnonzero(bitmap)
        disputed = active[owner[active] != -1]
        if disputed.size:
            prior_indices = {int(owner[pixel]) for pixel in disputed}
            for prior in sorted(prior_indices):
                result.conflicts.append(
                    ConflictRecord(
                        scene_id=scene_id,
                        kind="pixel_ownership",
                        winning_instance=spec[1],
                        losing_instance=retained[prior][1],
                        semantic_class=spec[0],
                        iou=None,
                        confidence_winner=spec[3],
                        confidence_loser=retained[prior][3],
                        sort_key=f"{scene_id}:pixel_ownership:{retained[prior][1]}:{spec[1]}",
                    )
                )
        free = active[owner[active] == -1]
        owner[free] = idx

    label_map = np.zeros(total_pixels, dtype=np.uint16)
    for idx, spec in enumerate(retained):
        semantic_class, fused_name, is_crowd, confidence, contributors, versions, bitmap, match_iou = spec
        pixels = np.flatnonzero(owner == idx)
        if pixels.size == 0:
            result.dropped_instances.append(fused_name)
            continue
        label_map[pixels] = idx + 1
        runs, pixel_count = bitmap_to_runs((owner == idx).reshape(height, width))
        result.fused_instances.append(
            FusedInstance(
                fused_id=f"fused-{idx + 1:04d}",
                scene_id=scene_id,
                semantic_class=semantic_class,
                width=width,
                height=height,
                runs=runs,
                pixel_count=pixel_count,
                confidence=confidence,
                is_crowd=is_crowd,
                contributors=contributors,
                source_versions=versions,
                match_iou=match_iou,
            )
        )

    result.label_map = label_map.reshape(height, width)
    result.conflicts.sort(key=lambda item: item.sort_key)
    result.boundary_probes.sort(key=lambda item: item.sort_key)
    return result


def _coverage_probe(
    scene_id: str,
    instance_id: str,
    pixel_count: int,
    total_pixels: int,
    min_coverage: float,
) -> BoundaryProbe:
    reference = float(pixel_count) / float(total_pixels)
    actual = float(np.float32(np.float32(pixel_count) / np.float32(total_pixels)))
    reference_decision = reference >= min_coverage
    actual_decision = actual >= min_coverage
    sensitive = reference_decision != actual_decision
    return BoundaryProbe(
        instance_id=instance_id,
        scene_id=scene_id,
        kind="policy_coverage",
        reference_value=reference,
        actual_value=actual,
        difference=abs(reference - actual),
        reference_decision=reference_decision,
        actual_decision=actual_decision,
        sensitive=sensitive,
        sort_key=f"{scene_id}:policy:{instance_id}",
    )


def iou_boundary_probe(
    scene_id: str,
    instance_id: str,
    bitmap_a: np.ndarray,
    bitmap_b: np.ndarray,
    intersection: int,
    union: int,
    iou_threshold: float,
) -> BoundaryProbe:
    reference = _reference_iou(intersection, union)
    actual = _actual_iou(bitmap_a, bitmap_b)
    reference_decision = reference >= iou_threshold
    actual_decision = actual >= float(np.float32(iou_threshold))
    sensitive = reference_decision != actual_decision
    return BoundaryProbe(
        instance_id=instance_id,
        scene_id=scene_id,
        kind="threshold_iou",
        reference_value=reference,
        actual_value=actual,
        difference=abs(reference - actual),
        reference_decision=reference_decision,
        actual_decision=actual_decision,
        sensitive=sensitive,
        sort_key=f"{scene_id}:threshold:{instance_id}",
    )
