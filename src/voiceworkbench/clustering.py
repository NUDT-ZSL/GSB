"""Stage 3+4: deterministic agglomerative clustering and segment assignment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .concurrency import BatchOutcome, Task, run_tasks
from .config import PipelineConfig
from .determinism import short_hash
from .errors import CommandError, FailureRecord
from .vad import SpeechSegment

EPS = 1e-12


@dataclass(frozen=True)
class Cluster:
    label: str
    member_ids: list[str]
    centroid: list[float]
    origin: str  # "agglomerative" | "merged" | "split"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "member_ids": list(self.member_ids),
            "centroid": [round(v, 6) for v in self.centroid],
            "origin": self.origin,
        }


@dataclass(frozen=True)
class Assignment:
    segment_id: str
    label: str
    score: float
    distance: float
    candidate_distances: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "label": self.label,
            "score": round(self.score, 6),
            "distance": round(self.distance, 6),
            "candidate_distances": {
                label: round(distance, 6)
                for label, distance in sorted(self.candidate_distances.items())
            },
        }


def _distance_matrix(segments: list[SpeechSegment]) -> np.ndarray:
    # Euclidean distance over the quantized integer voiceprint space.  The
    # integer codes make this metric stable and well separated across speakers
    # while cosine distance collapses near-identical directions.
    matrix = np.asarray(
        [segment.mean_voiceprint for segment in segments], dtype=np.float64,
    )
    squared = np.sum(matrix ** 2, axis=1)
    distances = squared[:, None] + squared[None, :] - 2.0 * (matrix @ matrix.T)
    np.maximum(distances, 0.0, out=distances)
    return np.sqrt(distances)


def _earliest_frame(member_indices: list[int], segments: list[SpeechSegment]) -> tuple[str, int, int]:
    earliest = min(member_indices, key=lambda idx: (segments[idx].file_name,
                                                    segments[idx].start_frame))
    seg = segments[earliest]
    return seg.segment_id, seg.start_frame, earliest


def agglomerative(segments: list[SpeechSegment], threshold: float) -> list[Cluster]:
    count = len(segments)
    if count == 0:
        return []
    distances = _distance_matrix(segments)
    groups: list[list[int]] = [[index] for index in range(count)]
    active = [True] * count

    while True:
        best: tuple[float, tuple[str, int, int], tuple[str, int, int], int, int] | None = None
        for i in range(count):
            if not active[i]:
                continue
            for j in range(i + 1, count):
                if not active[j]:
                    continue
                pair_distance = float(np.max(distances[np.ix_(groups[i], groups[j])]))
                if pair_distance > threshold:
                    continue
                tie_i = _earliest_frame(groups[i], segments)
                tie_j = _earliest_frame(groups[j], segments)
                # tie-break: closest distance, then earliest frame, then index
                candidate = (pair_distance,
                             min(tie_i, tie_j), max(tie_i, tie_j), i, j)
                if best is None or candidate[:3] < best[:3]:
                    best = candidate
        if best is None:
            break
        _, _, _, i, j = best
        groups[i] = sorted(groups[i] + groups[j])
        active[j] = False
        groups[j] = []

    surviving = [group for group in groups if group]
    surviving.sort(key=lambda group: _earliest_frame(group, segments)[:2])
    clusters: list[Cluster] = []
    for ordinal, group in enumerate(surviving):
        label = f"SPK-{ordinal:02d}"
        centroid = _centroid(group, segments)
        member_ids = sorted(
            (segments[index].segment_id for index in group),
            key=lambda sid: _segment_sort_key(sid, segments),
        )
        clusters.append(Cluster(label=label, member_ids=member_ids,
                                centroid=centroid, origin="agglomerative"))
    return clusters


def _segment_sort_key(segment_id: str, segments: list[SpeechSegment]) -> list[Any]:
    for segment in segments:
        if segment.segment_id == segment_id:
            return [segment.file_name, segment.start_frame, segment.segment_id]
    return ["", 0, segment_id]


def _centroid(member_indices: list[int], segments: list[SpeechSegment]) -> list[float]:
    matrix = np.asarray(
        [segments[index].mean_voiceprint for index in member_indices], dtype=np.float64,
    )
    return [float(v) for v in np.mean(matrix, axis=0)]


def assign_segments(segments: list[SpeechSegment], clusters: list[Cluster],
                    cfg: PipelineConfig,
                    fault_injection: dict[str, Any] | None = None
                    ) -> tuple[list[Assignment], list[FailureRecord]]:
    if not clusters:
        return [], []
    centroids = {
        cluster.label: np.asarray(cluster.centroid, dtype=np.float64)
        for cluster in clusters
    }
    fault = fault_injection or {}
    ordered = sorted(segments, key=lambda item: (item.file_name, item.start_frame,
                                                 item.segment_id))

    def worker(task: Task) -> Assignment:
        segment: SpeechSegment = task.payload["segment"]
        if "assign_fail" in fault and fault["assign_fail"] == segment.segment_id:
            raise RuntimeError("injected assignment failure")
        vector = np.asarray(segment.mean_voiceprint, dtype=np.float64)
        candidate: dict[str, float] = {}
        for label, centroid in centroids.items():
            candidate[label] = float(np.linalg.norm(vector - centroid))
        # nearest cluster; exact distance ties resolve by label lexicographic order
        label, distance = min(candidate.items(), key=lambda item: (item[1], item[0]))
        # normalize Euclidean distance by the voiceprint code range so the
        # assignment score stays within [0, 1] while preserving ordering
        code_span = float(cfg.quant_levels * np.sqrt(cfg.voiceprint_dims))
        score = 1.0 - distance / code_span
        return Assignment(
            segment_id=segment.segment_id,
            label=label,
            score=float(max(0.0, min(1.0, score))),
            distance=distance,
            candidate_distances=dict(candidate),
        )

    tasks = [
        Task(sort_key=[segment.file_name, segment.start_frame, segment.segment_id],
             identifier=f"{segment.segment_id}#assign", stage="assignment",
             payload={"segment": segment})
        for segment in ordered
    ]
    outcome: BatchOutcome = run_tasks(tasks, worker, cfg.max_workers)
    return outcome.results, outcome.failures


def relabel(clusters: list[Cluster], segments: list[SpeechSegment]) -> list[Cluster]:
    """Re-derive canonical SPK-xx labels ordered by earliest member frame."""
    def earliest_key(cluster: Cluster) -> tuple[str, int]:
        keys = [_segment_sort_key(member, segments) for member in cluster.member_ids]
        smallest = min(keys)
        return smallest[0], smallest[1]

    ordered = sorted(clusters, key=earliest_key)
    return [
        Cluster(label=f"SPK-{ordinal:02d}", member_ids=list(cluster.member_ids),
                centroid=list(cluster.centroid), origin=cluster.origin)
        for ordinal, cluster in enumerate(ordered)
    ]


def merge_clusters(clusters: list[Cluster], label_a: str, label_b: str,
                   segments: list[SpeechSegment]) -> list[Cluster]:
    if label_a == label_b:
        raise CommandError("merge requires two distinct labels", labels=[label_a, label_b])
    by_label = {cluster.label: cluster for cluster in clusters}
    for label in (label_a, label_b):
        if label not in by_label:
            raise CommandError(f"unknown cluster label: {label}", label=label)
    first, second = by_label[label_a], by_label[label_b]
    member_ids = sorted(
        set(first.member_ids) | set(second.member_ids),
        key=lambda sid: _segment_sort_key(sid, segments),
    )
    index_map = {segment.segment_id: index for index, segment in enumerate(segments)}
    centroid = _centroid([index_map[sid] for sid in member_ids], segments)
    merged = Cluster(label=first.label, member_ids=member_ids,
                     centroid=centroid, origin="merged")
    remaining = [cluster for cluster in clusters if cluster.label not in (label_a, label_b)]
    remaining.append(merged)
    return relabel(remaining, segments)


def split_cluster(clusters: list[Cluster], label: str,
                  segments: list[SpeechSegment]) -> list[Cluster]:
    target = next((cluster for cluster in clusters if cluster.label == label), None)
    if target is None:
        raise CommandError(f"unknown cluster label: {label}", label=label)
    index_map = {segment.segment_id: index for index, segment in enumerate(segments)}
    members = [index_map[sid] for sid in target.member_ids]
    if len(members) < 2:
        raise CommandError("cannot split a singleton cluster", label=label,
                           member_count=len(members))
    vectors = {index: np.asarray(segments[index].mean_voiceprint, dtype=np.float64)
               for index in members}
    # deterministic farthest pair as seeds, ties by earliest frame
    seed_a, seed_b = _farthest_seed_pair(members, vectors, segments)

    group_a = [seed_a]
    group_b = [seed_b]
    rest = [index for index in members if index not in (seed_a, seed_b)]
    for index in rest:
        dist_a = float(np.linalg.norm(vectors[index] - vectors[seed_a]))
        dist_b = float(np.linalg.norm(vectors[index] - vectors[seed_b]))
        key = _segment_sort_key(segments[index].segment_id, segments)
        if (dist_a, key) <= (dist_b, key):
            group_a.append(index)
        else:
            group_b.append(index)

    def build(group: list[int]) -> Cluster:
        member_ids = sorted(
            (segments[index].segment_id for index in group),
            key=lambda sid: _segment_sort_key(sid, segments),
        )
        return Cluster(label="", member_ids=member_ids,
                       centroid=_centroid(group, segments), origin="split")

    children = [build(group_a), build(group_b)]
    remaining = [cluster for cluster in clusters if cluster.label != label]
    remaining.extend(children)
    return relabel(remaining, segments)


def _farthest_seed_pair(members: list[int], vectors: dict[int, np.ndarray],
                        segments: list[SpeechSegment]) -> tuple[int, int]:
    best_pair: tuple[float, list[Any], list[Any], int, int] | None = None
    for pos, i in enumerate(members):
        for j in members[pos + 1:]:
            distance = float(np.linalg.norm(vectors[i] - vectors[j]))
            key_i = _segment_sort_key(segments[i].segment_id, segments)
            key_j = _segment_sort_key(segments[j].segment_id, segments)
            candidate = (-distance, key_i, key_j, i, j)
            if best_pair is None or candidate < best_pair:
                best_pair = candidate
    assert best_pair is not None
    return best_pair[3], best_pair[4]


def clusters_artifact(clusters: list[Cluster], assignments: list[Assignment]) -> dict[str, Any]:
    return {
        "clusters": [cluster.to_dict() for cluster in clusters],
        "assignments": [assignment.to_dict() for assignment in assignments],
        "clusters_fingerprint": short_hash(
            [cluster.to_dict() for cluster in clusters]
            + [assignment.to_dict() for assignment in assignments]
        ),
    }
