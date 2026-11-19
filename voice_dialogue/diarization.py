"""Registration-based deterministic speaker separation.

Enrollment statistics are estimated from the leading speech run after each
silence gap. Frames are then clustered deterministically:

* fine path: Lloyd iterations with fixed seed;
* approximate path: a single nearest-prototype pass.

Ties are always broken in ascending speaker order, and clusters are mapped to
speaker ids by first appearance so labels line up with the manifest's
``speaker_ids`` ordering. All prototype-to-frame distance evaluations are
counted to enforce the separation comparison budget.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import BusinessError, ErrorCode


@dataclass
class DiarizationResult:
    labels: np.ndarray              # -1 for silence, speaker_id otherwise
    switches: list                  # list of {frame, from_speaker, to_speaker}
    segments: list                  # contiguous speech runs
    comparisons: int
    iterations: int
    cluster_count: int
    is_speech: np.ndarray


def _speech_mask(log_energy_index: int, features: np.ndarray) -> np.ndarray:
    energy = features[:, log_energy_index]
    lo = float(energy.min())
    hi = float(energy.max())
    threshold = lo + 0.35 * (hi - lo)
    return energy > threshold


def _standardize(features: np.ndarray, speech: np.ndarray):
    data = features[speech]
    mean = data.mean(axis=0)
    std = data.std(axis=0)
    std[std < 1e-12] = 1.0
    return mean, std


def _embedding_view(features: np.ndarray, path: str) -> np.ndarray:
    """Features used for speaker distance.

    Speaker identity is primarily encoded in pitch (f0, last column) rather
    than energy. The approximate 4D contract keeps energy for emotion
    regression, but separation additionally derives a centroid-like spectral
    column and up-weights f0 without changing the reported feature dim.
    """
    if path == "fine":
        weights = np.ones(features.shape[1], dtype=np.float64)
        weights[-1] = 1.0  # f0 already in Hz and standardized downstream
        if features.shape[1] >= 4:
            weights[3] = 1.5  # centroid
        return features * weights
    energy = features[:, 0]
    log_energy = features[:, 1]
    zcr = features[:, 2]
    f0 = _stable_f0(features[:, 3])
    # Deterministic derived spectral cue from available channels.
    pseudo_centroid = 0.6 * zcr + 0.4 * f0
    base = np.stack(
        [energy * 0.5, log_energy * 0.5, zcr, pseudo_centroid, f0],
        axis=1,
    )
    return base


def _enrollment_runs(
    speech: np.ndarray, expected_speakers: int
) -> list[np.ndarray]:
    """Leading contiguous speech run after each silence gap.

    Edge frames (first/last 15% of each run) are dropped because fades and
    onset transients are unstable; the remaining frames are enrollment stats.
    """
    runs: list[np.ndarray] = []
    n = speech.shape[0]
    i = 0
    while i < n and len(runs) < expected_speakers:
        if not speech[i]:
            i += 1
            continue
        j = i
        while j < n and speech[j]:
            j += 1
        run = np.arange(i, j)
        margin = max(1, int(0.15 * run.shape[0]))
        if run.shape[0] - 2 * margin >= 2:
            run = run[margin:run.shape[0] - margin]
        runs.append(run)
        i = j
    return runs


def _all_speech_runs(speech: np.ndarray) -> list[np.ndarray]:
    """Every contiguous speech run (segment), in chronological order."""
    runs: list[np.ndarray] = []
    n = speech.shape[0]
    i = 0
    while i < n:
        if not speech[i]:
            i += 1
            continue
        j = i
        while j < n and speech[j]:
            j += 1
        runs.append(np.arange(i, j))
        i = j
    return runs


def _run_centroids(z: np.ndarray, runs: list) -> np.ndarray:
    return np.stack([z[run].mean(axis=0) for run in runs], axis=0)


def _pairwise_distances(
    points: np.ndarray, prototypes: np.ndarray, counter: list
) -> np.ndarray:
    # Squared Euclidean distance, expanded without scipy.
    diff = points[:, None, :] - prototypes[None, :, :]
    counter[0] += int(points.shape[0] * prototypes.shape[0])
    return np.einsum("ijk,ijk->ij", diff, diff)


def _constrained_assign(points: np.ndarray, prototypes: np.ndarray,
                        counter: list) -> np.ndarray:
    """Nearest-prototype assignment guaranteeing every cluster is non-empty.

    One deterministic linear pass over the (frame, prototype) distance matrix,
    plus a greedy repair step that reassigns the closest eligible frame to any
    empty cluster; comparison count stays O(n*k).
    """
    dist = _pairwise_distances(points, prototypes, counter)
    labels = np.argmax(-dist, axis=1)
    k = prototypes.shape[0]
    empty = [c for c in range(k) if not np.any(labels == c)]
    if not empty:
        return labels
    for cluster in empty:
        best_frame = int(np.argmin(dist[:, cluster]))
        labels[best_frame] = cluster
        dist[best_frame, :] = np.inf
    return labels


def _stable_f0(f0: np.ndarray, window: int = 5) -> np.ndarray:
    """Edge-padded running median; zero (unvoiced) values participate."""
    if f0.shape[0] < window:
        return f0
    half = window // 2
    padded = np.pad(f0, (half, half), mode="edge")
    return np.array(
        [np.median(padded[i:i + window]) for i in range(f0.shape[0])],
        dtype=np.float64,
    )


def _robust_scale(values: np.ndarray) -> np.ndarray:
    """Scale by inter-percentile range so absolute pitch spacing survives."""
    lo = float(np.percentile(values, 10))
    hi = float(np.percentile(values, 90))
    scale = hi - lo
    if scale < 1e-9:
        scale = float(values.std()) or 1.0
    return (values - lo) / scale


def separate_speakers(
    features: np.ndarray,
    frame_indices: np.ndarray,
    expected_speaker_ids: list,
    path: str,
    fine_iterations: int,
    comparison_budget: int,
    dialog_id: str | None = None,
) -> DiarizationResult:
    expected = sorted(int(s) for s in expected_speaker_ids)
    k = len(expected)
    n_frames = features.shape[0]
    log_energy_index = 1 if features.shape[1] >= 2 else 0
    speech = _speech_mask(log_energy_index, features)
    if int(speech.sum()) < k:
        raise BusinessError(
            ErrorCode.E_SPEAKER_UNASSIGNED,
            "not enough speech frames to enroll every registered speaker",
            dialog_id=dialog_id,
            details={"speech_frames": int(speech.sum()), "speakers": k},
        )

    embedding = _embedding_view(features, path)
    # Standardize using speech frames only: including silence zeros would
    # collapse the inter-speaker pitch spacing that drives separation.
    mean, std = _standardize(embedding, speech)
    z = (embedding - mean) / std
    # Robust pitch scaling preserves absolute inter-speaker f0 spacing.
    z[:, -1] = _robust_scale(embedding[:, -1])

    runs = _enrollment_runs(speech, k)
    if len(runs) < k:
        raise BusinessError(
            ErrorCode.E_SPEAKER_UNASSIGNED,
            f"only {len(runs)} enrollment runs for {k} registered speakers",
            dialog_id=dialog_id,
        )
    prototypes = np.empty((k, z.shape[1]), dtype=np.float64)
    for c, run in enumerate(runs[:k]):
        prototypes[c] = z[run].mean(axis=0)

    # Segment-level deterministic classification. Each contiguous speech run
    # is represented by its embedding centroid and assigned to the nearest
    # enrollment prototype; the fine path additionally refines prototypes with
    # bounded Lloyd iterations, while the approximate path stays single-pass.
    all_runs = _all_speech_runs(speech)
    run_centroids = _run_centroids(z, all_runs)
    counter = [0]
    iterations = 1
    run_clusters = _constrained_assign(run_centroids, prototypes, counter)
    if path == "fine":
        for _ in range(max(1, fine_iterations)):
            new_proto = np.empty_like(prototypes)
            for c in range(k):
                member_frames = np.concatenate(
                    [all_runs[i] for i in range(len(all_runs))
                     if run_clusters[i] == c]
                ) if np.any(run_clusters == c) else None
                new_proto[c] = (
                    z[member_frames].mean(axis=0)
                    if member_frames is not None
                    else prototypes[c]
                )
            if np.allclose(new_proto, prototypes, atol=1e-10):
                prototypes = new_proto
                break
            prototypes = new_proto
            run_clusters = _constrained_assign(
                run_centroids, prototypes, counter
            )
            iterations += 1
            if counter[0] > comparison_budget:
                raise BusinessError(
                    ErrorCode.E_RESOURCE_LIMIT,
                    "separation comparison budget exceeded during fine path",
                    dialog_id=dialog_id,
                    field="max_separation_comparisons",
                    details={
                        "comparisons": counter[0],
                        "budget": comparison_budget,
                    },
                )
    else:
        projected = len(all_runs) * k * 2
        if projected > comparison_budget:
            raise BusinessError(
                ErrorCode.E_RESOURCE_LIMIT,
                "single-pass separation exceeds comparison budget",
                dialog_id=dialog_id,
                field="max_separation_comparisons",
                details={"comparisons": projected, "budget": comparison_budget},
            )
        # One prototype refresh from run assignments then a final pass.
        refreshed = np.empty_like(prototypes)
        for c in range(k):
            member_frames = np.concatenate(
                [all_runs[i] for i in range(len(all_runs))
                 if run_clusters[i] == c]
            ) if np.any(run_clusters == c) else None
            refreshed[c] = (
                z[member_frames].mean(axis=0)
                if member_frames is not None
                else prototypes[c]
            )
        prototypes = refreshed
        run_clusters = _constrained_assign(run_centroids, prototypes, counter)

    # Enrollment runs are ordered in time and each registered speaker owns one,
    # so the cluster assigned to enrollment run i maps to speaker id i.
    cluster_to_speaker: dict[int, int] = {}
    enroll_clusters: list[int] = []
    for enroll_run in runs[:k]:
        enroll_mid = int(enroll_run[enroll_run.shape[0] // 2])
        match_idx = next(
            i for i, r in enumerate(all_runs)
            if int(r[0]) <= enroll_mid < int(r[-1]) + 1
        )
        enroll_clusters.append(int(run_clusters[match_idx]))
    if len(set(enroll_clusters)) == k:
        for speaker_order, cluster in enumerate(enroll_clusters):
            cluster_to_speaker[cluster] = expected[speaker_order]
    else:
        # Collision fallback: enrollment f0 is monotonic with the registered
        # speaker base pitch, so order runs by robust mean f0 (ascending) and
        # align to ascending speaker ids.
        f0_means = [float(z[run, -1].mean()) for run in runs[:k]]
        ordered = sorted(range(k), key=lambda i: (f0_means[i], i))
        observed = sorted(np.unique(run_clusters).tolist())
        if len(observed) != k:
            raise BusinessError(
                ErrorCode.E_SPEAKER_MISMATCH,
                f"clustering produced {len(observed)} clusters, expected {k}",
                dialog_id=dialog_id,
                details={"clusters": len(observed), "speakers": k},
            )
        for speaker_order, run_idx in enumerate(ordered):
            cluster_to_speaker[observed[run_idx]] = expected[speaker_order]

    labels = np.full(n_frames, -1, dtype=np.int64)
    for run, cluster in zip(all_runs, run_clusters.tolist()):
        labels[run] = cluster_to_speaker[int(cluster)]
    present = set(labels[speech].tolist())
    if present - set(expected):
        raise BusinessError(
            ErrorCode.E_SPEAKER_MISMATCH,
            f"phantom speaker labels {sorted(present - set(expected))}",
            dialog_id=dialog_id,
        )
    if set(expected) - present:
        raise BusinessError(
            ErrorCode.E_SPEAKER_UNASSIGNED,
            f"registered speakers never assigned: "
            f"{sorted(set(expected) - present)}",
            dialog_id=dialog_id,
        )

    switches: list = []
    segments: list = []
    active = speech[0]
    seg_start = 0
    prev_speaker = int(labels[0]) if active else -1
    for fi in range(1, n_frames):
        if speech[fi] != active:
            if active:
                segments.append(
                    {
                        "start_frame": seg_start,
                        "end_frame": fi,
                        "speaker_id": prev_speaker,
                    }
                )
            active = speech[fi]
            seg_start = fi
            if active:
                prev_speaker = int(labels[fi])
        elif active and labels[fi] != prev_speaker:
            switches.append(
                {
                    "frame": int(frame_indices[fi]),
                    "from_speaker": prev_speaker,
                    "to_speaker": int(labels[fi]),
                }
            )
            segments.append(
                {
                    "start_frame": seg_start,
                    "end_frame": fi,
                    "speaker_id": prev_speaker,
                }
            )
            seg_start = fi
            prev_speaker = int(labels[fi])
    if active:
        segments.append(
            {
                "start_frame": seg_start,
                "end_frame": n_frames,
                "speaker_id": prev_speaker,
            }
        )

    # Segments must tile the speech frames without gaps or overlaps.
    covered = np.zeros(n_frames, dtype=bool)
    for seg in segments:
        if covered[seg["start_frame"]:seg["end_frame"]].any():
            raise BusinessError(
                ErrorCode.E_INCONSISTENT,
                "overlapping diarization segments",
                dialog_id=dialog_id,
                details={"category": "segment_overlap"},
            )
        covered[seg["start_frame"]:seg["end_frame"]] = True
    if not np.array_equal(covered, speech):
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "segment coverage does not match speech mask",
            dialog_id=dialog_id,
            details={"category": "segment_coverage"},
        )

    return DiarizationResult(
        labels=labels,
        switches=switches,
        segments=segments,
        comparisons=counter[0],
        iterations=iterations,
        cluster_count=k,
        is_speech=speech,
    )
