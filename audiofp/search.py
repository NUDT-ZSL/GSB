from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from .config import TaskConfig, TrackSpec
from .errors import fail
from .store import ChunkStore
from .util import sha256_value


Vote = dict[str, Any]


def candidate_chunk_version(
    config: TaskConfig,
    query: TrackSpec,
    query_chunk_version: str,
    reference: TrackSpec,
    reference_version: str,
    start_frame: int,
    end_frame: int,
) -> str:
    return sha256_value(
        {
            "kind": "candidate_chunk_v1",
            "query": query.info.label,
            "query_chunk_version": query_chunk_version,
            "reference": reference.info.label,
            "reference_version": reference_version,
            "frame_interval": [start_frame, end_frame],
            "fingerprint": config.fingerprint.canonical(),
            "search": config.search.canonical(),
        }
    )


def _offset_values(
    config: TaskConfig,
    query: TrackSpec,
    reference: TrackSpec,
    query_anchor_frame: int,
    reference_anchor_frame: int,
) -> tuple[float, int]:
    q_hop = config.fingerprint.hop_length
    ref_hop = config.fingerprint.hop_length
    q_rate = query.info.sample_rate
    ref_rate = reference.info.sample_rate
    offset_samples = (
        reference_anchor_frame * ref_hop
        - query_anchor_frame * q_hop * (ref_rate / float(q_rate))
    )
    offset_frames = offset_samples / float(ref_hop)
    return float(offset_samples), int(round(offset_frames))


def select_offset_modes(
    histogram: Counter[int],
    window_frames: int,
    *,
    min_count: int = 1,
    max_modes: int = 16,
) -> list[tuple[int, int]]:
    if not histogram:
        return []
    radius = max(1, int(window_frames) // 2)
    offsets = sorted(histogram)
    values = [int(histogram[offset]) for offset in offsets]
    prefix = [0]
    for value in values:
        prefix.append(prefix[-1] + value)
    candidates: list[tuple[int, int]] = []
    left = 0
    right = 0
    for index, offset in enumerate(offsets):
        while left < len(offsets) and offsets[left] < offset - radius:
            left += 1
        if right < index:
            right = index
        while right + 1 < len(offsets) and offsets[right + 1] <= offset + radius:
            right += 1
        density = prefix[right + 1] - prefix[left]
        if density >= min_count:
            candidates.append((density, offset))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[int, int]] = []
    for density, offset in candidates:
        if any(abs(offset - chosen[1]) <= window_frames for chosen in selected):
            continue
        selected.append((density, offset))
        if len(selected) >= max_modes:
            break
    selected.sort(key=lambda item: item[1])
    return selected


def _load_fingerprint_items(
    store: ChunkStore, track: TrackSpec, start: int, end: int, version: str
) -> list[dict[str, Any]]:
    logical = f"fp/{track.track_id}/{start:08d}_{end:08d}"
    result = store.read_valid(logical, version)
    if result.status != "valid":
        fail(
            "E_CACHE_READ",
            "读取指纹分块失败",
            track=track.info.label,
            frame_interval=[start, end],
            status=result.status,
            reason=result.reason,
        )
    return result.payload.get("items", [])


def build_candidate_payloads(
    config: TaskConfig,
    store: ChunkStore,
    query: TrackSpec,
    query_chunk: tuple[int, int, str],
    query_fingerprint_chunks: list[tuple[int, int, str]],
    references: Iterable[TrackSpec],
    reference_versions: dict[str, str],
    reference_index: dict[str, list[tuple[Any, ...]]],
    target_ref_ids: set[str],
) -> dict[str, dict[str, Any]]:
    if not target_ref_ids:
        return {}
    start_frame, end_frame, query_chunk_version = query_chunk
    items: list[dict[str, Any]] = []
    for fp_start, fp_end, fp_version in query_fingerprint_chunks:
        for item in _load_fingerprint_items(
            store, query, fp_start, fp_end, fp_version
        ):
            if start_frame <= int(item["anchor_frame"]) < end_frame:
                items.append(item)
    refs_by_id = {track.track_id: track for track in references}
    targets = {track_id: refs_by_id[track_id] for track_id in target_ref_ids}
    histograms: dict[str, Counter[int]] = {
        track_id: Counter() for track_id in target_ref_ids
    }
    hop = config.fingerprint.hop_length
    window = config.search.vote_window_frames

    for item in items:
        matches = reference_index.get(str(item["key"]))
        if not matches:
            continue
        query_anchor_frame = int(item["anchor_frame"])
        for row in matches:
            ref_id = str(row[1])
            reference = targets.get(ref_id)
            if reference is None:
                continue
            _, offset_round = _offset_values(
                config, query, reference, query_anchor_frame, int(row[3])
            )
            histograms[ref_id][offset_round] += 1

    modes_by_ref: dict[str, list[tuple[int, int]]] = {}
    mode_values_by_ref: dict[str, list[int]] = {}
    for ref_id, histogram in histograms.items():
        modes = select_offset_modes(
            histogram,
            window,
            min_count=max(1, config.search.min_matches // 4),
            max_modes=16,
        )
        modes_by_ref[ref_id] = modes
        mode_values_by_ref[ref_id] = [mode for _, mode in modes]

    radius = max(1, window // 2)
    groups_by_ref: dict[str, dict[tuple[int, int], dict[str, Any]]] = {
        track_id: {} for track_id in target_ref_ids
    }
    scale = config.search.fixed_scale
    for item in items:
        matches = reference_index.get(str(item["key"]))
        if not matches:
            continue
        query_anchor_frame = int(item["anchor_frame"])
        query_target_frame = int(item["target_frame"])
        for row in matches:
            ref_id = str(row[1])
            reference = targets.get(ref_id)
            if reference is None:
                continue
            modes = mode_values_by_ref[ref_id]
            if not modes:
                continue
            reference_anchor_frame = int(row[3])
            reference_target_frame = int(row[4])
            offset_samples, offset_round = _offset_values(
                config, query, reference, query_anchor_frame, reference_anchor_frame
            )
            if not any(abs(offset_round - mode) <= radius for mode in modes):
                continue
            weight = float(row[8])
            fixed_weight = max(1, int(round(weight * scale)))
            group_key = (query_anchor_frame, offset_round)
            group = groups_by_ref[ref_id].get(group_key)
            if group is None:
                group = {
                    "representative_key": str(item["key"]),
                    "query_anchor_frame": query_anchor_frame,
                    "query_target_frame_min": query_target_frame,
                    "query_target_frame_max": query_target_frame,
                    "reference_anchor_frame_min": reference_anchor_frame,
                    "reference_anchor_frame_max": reference_anchor_frame,
                    "reference_target_frame_min": reference_target_frame,
                    "reference_target_frame_max": reference_target_frame,
                    "offset_samples_sum": 0.0,
                    "offset_frame_round": int(offset_round),
                    "collision_count": 0,
                    "reference_weight": 0.0,
                    "fixed_weight_units": 0,
                    "key_occurrences": int(row[9]),
                    "weight_bins": {},
                }
                groups_by_ref[ref_id][group_key] = group
            group["query_target_frame_min"] = min(
                group["query_target_frame_min"], query_target_frame
            )
            group["query_target_frame_max"] = max(
                group["query_target_frame_max"], query_target_frame
            )
            group["reference_anchor_frame_min"] = min(
                group["reference_anchor_frame_min"], reference_anchor_frame
            )
            group["reference_anchor_frame_max"] = max(
                group["reference_anchor_frame_max"], reference_anchor_frame
            )
            group["reference_target_frame_min"] = min(
                group["reference_target_frame_min"], reference_target_frame
            )
            group["reference_target_frame_max"] = max(
                group["reference_target_frame_max"], reference_target_frame
            )
            group["offset_samples_sum"] += float(offset_samples) * fixed_weight
            group["collision_count"] += 1
            group["reference_weight"] += weight
            group["fixed_weight_units"] += fixed_weight
            group["representative_key"] = min(
                group["representative_key"], str(item["key"])
            )
            group["key_occurrences"] = max(
                group["key_occurrences"], int(row[9])
            )
            bin_key = f"{fixed_weight}:{weight:.17g}"
            group["weight_bins"][bin_key] = (
                int(group["weight_bins"].get(bin_key, 0)) + 1
            )

    payloads: dict[str, dict[str, Any]] = {}
    for ref_id, reference in targets.items():
        votes = list(groups_by_ref[ref_id].values())
        for vote in votes:
            if vote["fixed_weight_units"] > 0:
                vote["offset_samples_mean"] = (
                    vote["offset_samples_sum"] / vote["fixed_weight_units"]
                )
            else:
                vote["offset_samples_mean"] = 0.0
        votes.sort(
            key=lambda vote: (
                vote["query_anchor_frame"],
                vote["query_target_frame_min"],
                vote["reference_anchor_frame_min"],
                vote["reference_target_frame_min"],
                vote["representative_key"],
            )
        )
        payloads[ref_id] = {
            "schema_version": 1,
            "query": {
                "track_id": query.track_id,
                "file": query.info.label,
                "sample_rate": query.info.sample_rate,
                "order": query.order,
                "sample_count": query.info.sample_count,
            },
            "reference": {
                "track_id": reference.track_id,
                "file": reference.info.label,
                "sample_rate": reference.info.sample_rate,
                "order": reference.order,
                "sample_count": reference.info.sample_count,
            },
            "query_frame_interval": [start_frame, end_frame],
            "query_chunk_version": query_chunk_version,
            "reference_version": reference_versions[ref_id],
            "search": config.search.canonical(),
            "fingerprint": config.fingerprint.canonical(),
            "offset_histogram": [[offset, count] for offset, count in sorted(histograms[ref_id].items())],
            "selected_modes": [mode for _, mode in modes_by_ref[ref_id]],
            "vote_encoding": "collision_groups_v1",
            "votes": votes,
        }
    return payloads


def _weighted_median_offset(votes: list[Vote]) -> float:
    ordered = sorted(votes, key=lambda vote: vote["offset_samples_mean"])
    total = sum(int(vote["fixed_weight_units"]) for vote in ordered)
    if total <= 0:
        return float(ordered[len(ordered) // 2]["offset_samples_mean"])
    target = total / 2.0
    running = 0
    for vote in ordered:
        running += int(vote["fixed_weight_units"])
        if running >= target:
            return float(vote["offset_samples_mean"])
    return float(ordered[-1]["offset_samples_mean"])


def _frame_clusters(
    hit_weights: dict[int, int], window_frames: int
) -> list[list[int]]:
    if not hit_weights:
        return []
    frames = sorted(hit_weights)
    gap_limit = max(2, int(window_frames) * 2)
    clusters: list[list[int]] = []
    current: list[int] = [frames[0]]
    for frame in frames[1:]:
        if frame - current[-1] > gap_limit:
            clusters.append(current)
            current = [frame]
        else:
            current.append(frame)
    clusters.append(current)
    return clusters


def _refine_hit_range(
    hit_weights: dict[int, int],
    hop: int,
    frame_length: int,
    sample_count: int,
    window_frames: int,
) -> tuple[int, int]:
    if not hit_weights:
        return (0, 0)
    clusters = _frame_clusters(hit_weights, window_frames)
    selected = max(
        clusters,
        key=lambda cluster: (
            sum(hit_weights[frame] for frame in cluster),
            len(cluster),
            -cluster[0],
        ),
    )
    refined_first = selected[0]
    refined_last = selected[-1]
    start_sample = max(0, int(refined_first * hop))
    end_sample = min(sample_count, int(refined_last * hop + frame_length))
    if end_sample <= start_sample:
        end_sample = min(sample_count, start_sample + frame_length)
    return (start_sample, end_sample)


def _build_score_trace(
    votes: list[Vote], fixed_scale: int, tolerance: float
) -> tuple[list[dict[str, Any]], int, float, int | None, float]:
    fixed_total = 0
    reference_total = 0.0
    matched_total = 0
    first_exceeded: int | None = None
    maximum_difference = 0.0
    trace: list[dict[str, Any]] = []
    ordered = sorted(
        votes,
        key=lambda vote: (
            vote["query_anchor_frame"],
            vote["query_target_frame_min"],
            vote["reference_anchor_frame_min"],
            vote["reference_target_frame_min"],
            vote["representative_key"],
        ),
    )
    for vote in ordered:
        group_fixed_start = fixed_total
        group_reference_start = reference_total
        group_match_start = matched_total
        bins = sorted(
            (
                (float(key.split(":", 1)[0]), float(key.split(":", 1)[1]), int(count))
                for key, count in vote["weight_bins"].items()
            ),
            key=lambda item: (item[1], item[0]),
        )
        for fixed_unit, reference_weight, count in bins:
            for _ in range(count):
                fixed_total += int(fixed_unit)
                reference_total += reference_weight
                matched_total += 1
                if first_exceeded is None:
                    current_difference = abs(
                        fixed_total / float(fixed_scale) - reference_total
                    )
                    if current_difference > tolerance:
                        first_exceeded = matched_total
        fixed_as_float = fixed_total / float(fixed_scale)
        difference = abs(fixed_as_float - reference_total)
        maximum_difference = max(maximum_difference, difference)
        trace.append(
            {
                "match_index": matched_total,
                "collision_count": matched_total - group_match_start,
                "representative_key": vote["representative_key"],
                "group_fixed_weight_units": fixed_total - group_fixed_start,
                "group_reference_weight": reference_total - group_reference_start,
                "fixed_cumulative": fixed_as_float,
                "reference_cumulative": reference_total,
                "absolute_difference": difference,
                "tolerance_exceeded": difference > tolerance,
            }
        )
    return trace, fixed_total, reference_total, first_exceeded, maximum_difference


def aggregate_query_candidates(
    config: TaskConfig,
    query: TrackSpec,
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_ref: dict[str, list[Vote]] = defaultdict(list)
    ref_meta: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        ref_id = str(payload["reference"]["track_id"])
        ref_meta[ref_id] = payload["reference"]
        by_ref[ref_id].extend(payload.get("votes", []))

    window = config.search.vote_window_frames
    radius = max(1, window // 2)
    candidates: list[dict[str, Any]] = []
    diagnostics = {
        "vote_count": sum(
            int(vote["collision_count"])
            for votes in by_ref.values()
            for vote in votes
        ),
        "histogram_modes": {},
    }
    for ref_id, votes in by_ref.items():
        if sum(int(vote["collision_count"]) for vote in votes) < config.search.min_matches:
            diagnostics["histogram_modes"][ref_id] = []
            continue
        histogram: Counter[int] = Counter()
        for vote in votes:
            histogram[int(vote["offset_frame_round"])] += int(
                vote["collision_count"]
            )
        modes = select_offset_modes(
            histogram,
            window,
            min_count=config.search.min_matches,
            max_modes=32,
        )
        diagnostics["histogram_modes"][ref_id] = [mode for _, mode in modes]
        seen_candidate_ids: set[str] = set()
        for _, mode in modes:
            selected = [
                vote
                for vote in votes
                if abs(int(vote["offset_frame_round"]) - mode) <= radius
            ]
            selected.sort(
                key=lambda vote: (
                    vote["query_anchor_frame"],
                    vote["query_target_frame_min"],
                    vote["reference_anchor_frame_min"],
                    vote["reference_target_frame_min"],
                    vote["representative_key"],
                )
            )
            if not selected:
                continue
            groups: list[list[Vote]] = []
            current: list[Vote] = []
            last_anchor: int | None = None
            gap_limit = max(2, window * 4)
            for vote in selected:
                anchor = int(vote["query_anchor_frame"])
                if last_anchor is not None and anchor - last_anchor > gap_limit:
                    groups.append(current)
                    current = []
                current.append(vote)
                last_anchor = anchor
            if current:
                groups.append(current)

            for group in groups:
                deduplicated: dict[tuple[Any, ...], Vote] = {}
                for vote in group:
                    identity = (
                        vote["query_anchor_frame"],
                        vote["query_target_frame_min"],
                        vote["query_target_frame_max"],
                        vote["reference_anchor_frame_min"],
                        vote["reference_anchor_frame_max"],
                        vote["representative_key"],
                    )
                    deduplicated[identity] = vote
                base_votes = list(deduplicated.values())
                q_hits_all: dict[int, int] = defaultdict(int)
                for vote in base_votes:
                    q_hits_all[int(vote["query_anchor_frame"])] += int(
                        vote["collision_count"]
                    )
                q_clusters = _frame_clusters(q_hits_all, window)
                minimum_fixed_units = (
                    config.search.min_matches * config.search.fixed_scale
                )
                for q_cluster in q_clusters:
                    cluster_first, cluster_last = q_cluster[0], q_cluster[-1]
                    unique_group = [
                        vote
                        for vote in base_votes
                        if cluster_first
                        <= int(vote["query_anchor_frame"])
                        <= cluster_last
                    ]
                    total_matches = sum(
                        int(vote["collision_count"]) for vote in unique_group
                    )
                    if total_matches < config.search.min_matches:
                        continue
                    (
                        trace,
                        fixed_units,
                        reference_score,
                        first_exceeded,
                        max_diff,
                    ) = _build_score_trace(
                        unique_group,
                        config.search.fixed_scale,
                        config.search.score_tolerance,
                    )
                    if fixed_units < minimum_fixed_units:
                        continue
                    q_hits: dict[int, int] = defaultdict(int)
                    r_hits: dict[int, int] = defaultdict(int)
                    for vote in unique_group:
                        collision_count = int(vote["collision_count"])
                        q_hits[int(vote["query_anchor_frame"])] += collision_count
                        r_hits[int(vote["reference_anchor_frame_min"])] += collision_count
                        r_hits[int(vote["reference_anchor_frame_max"])] += collision_count
                    q_start, q_end = _refine_hit_range(
                        q_hits,
                        config.fingerprint.hop_length,
                        config.fingerprint.frame_length,
                        query.info.sample_count,
                        window,
                    )
                    r_start, r_end = _refine_hit_range(
                        r_hits,
                        config.fingerprint.hop_length,
                        config.fingerprint.frame_length,
                        ref_meta[ref_id]["sample_count"],
                        window,
                    )
                    if q_end <= q_start or r_end <= r_start:
                        continue
                    offset_samples = _weighted_median_offset(unique_group)
                    ref_hop = config.fingerprint.hop_length
                    frame_offset = int(round(offset_samples / float(ref_hop)))
                    reference_offset_samples = float(frame_offset * ref_hop)
                    continuous_residual = abs(reference_offset_samples - offset_samples)
                    candidate_id = (
                        f"{query.track_id}:{ref_id}:{q_start}:{q_end}:{r_start}:{r_end}:"
                        f"{frame_offset}"
                    )
                    if candidate_id in seen_candidate_ids:
                        continue
                    seen_candidate_ids.add(candidate_id)
                    candidates.append(
                        {
                        "candidate_id": candidate_id,
                        "query_file": query.info.label,
                        "query_track_id": query.track_id,
                        "query_order": query.order,
                        "reference_file": ref_meta[ref_id]["file"],
                        "reference_track_id": ref_id,
                        "reference_order": ref_meta[ref_id]["order"],
                        "query_start_sample": int(q_start),
                        "query_end_sample": int(q_end),
                        "reference_start_sample": int(r_start),
                        "reference_end_sample": int(r_end),
                        "offset_mode_frame": int(mode),
                        "integer_frame_offset": int(frame_offset),
                        "float_reference_offset_samples": reference_offset_samples,
                        "continuous_offset_samples": float(offset_samples),
                        "offset_conversion_error_samples": 0.0,
                        "continuous_offset_residual_samples": float(
                            continuous_residual
                        ),
                        "time_offset_tolerance_samples": float(
                            config.search.time_offset_tolerance_samples
                        ),
                        "match_count": int(total_matches),
                        "minimum_matches": config.search.min_matches,
                        "minimum_fixed_score_units": int(minimum_fixed_units),
                        "fixed_score_units": int(fixed_units),
                        "fixed_score": fixed_units / float(config.search.fixed_scale),
                        "reference_float_score": float(reference_score),
                        "score_difference": abs(
                            fixed_units / float(config.search.fixed_scale)
                            - reference_score
                        ),
                        "score_tolerance": float(config.search.score_tolerance),
                        "first_tolerance_exceeded_at": first_exceeded,
                        "maximum_score_difference": float(max_diff),
                        "score_trace": trace,
                        "suppressed": False,
                        "suppressed_by": None,
                        }
                    )

    if config.search.relative_score_floor > 0 and candidates:
        maximum_fixed_units = max(
            candidate["fixed_score_units"] for candidate in candidates
        )
        floor_units = maximum_fixed_units * config.search.relative_score_floor
        candidates = [
            candidate
            for candidate in candidates
            if candidate["fixed_score_units"] >= floor_units
        ]

    candidates.sort(
        key=lambda candidate: (
            -candidate["fixed_score_units"],
            -candidate["match_count"],
            -(candidate["query_end_sample"] - candidate["query_start_sample"]),
            candidate["query_order"],
            candidate["reference_order"],
            candidate["query_start_sample"],
            candidate["reference_start_sample"],
            candidate["candidate_id"],
        )
    )
    for rank, candidate in enumerate(candidates, start=1):
        candidate["fixed_rank"] = rank

    float_order = sorted(
        candidates,
        key=lambda candidate: (
            -candidate["reference_float_score"],
            -candidate["match_count"],
            candidate["candidate_id"],
        ),
    )
    float_rank = {
        candidate["candidate_id"]: rank
        for rank, candidate in enumerate(float_order, start=1)
    }
    for candidate in candidates:
        candidate["reference_float_rank"] = float_rank[candidate["candidate_id"]]
        candidate["fixed_float_rank_mismatch"] = (
            candidate["fixed_rank"] != candidate["reference_float_rank"]
        )
    mismatch = [
        {
            "candidate_id": candidate["candidate_id"],
            "fixed_rank": candidate["fixed_rank"],
            "reference_float_rank": candidate["reference_float_rank"],
            "fixed_score": candidate["fixed_score"],
            "reference_float_score": candidate["reference_float_score"],
        }
        for candidate in candidates
        if candidate["fixed_float_rank_mismatch"]
    ]

    accepted: list[dict[str, Any]] = []
    for candidate in candidates:
        overlapping = None
        for winner in accepted:
            left = max(candidate["query_start_sample"], winner["query_start_sample"])
            right = min(candidate["query_end_sample"], winner["query_end_sample"])
            if right > left:
                overlapping = winner
                break
        if overlapping is not None:
            candidate["suppressed"] = True
            candidate["suppressed_by"] = overlapping["candidate_id"]
            continue
        accepted.append(candidate)
    for final_rank, candidate in enumerate(accepted, start=1):
        candidate["final_rank"] = final_rank
    for candidate in candidates:
        if candidate["suppressed"]:
            candidate["final_rank"] = None

    diagnostics["rank_mismatches"] = mismatch
    diagnostics["candidate_count_before_resolution"] = len(candidates)
    diagnostics["candidate_count_after_resolution"] = len(accepted)
    if not accepted:
        diagnostics["unmatched"] = {
            "code": "E_NO_REFERENCE_COVERAGE",
            "message": "待检曲目未被任何参考曲目在任一估计偏移覆盖",
            "vote_count": diagnostics["vote_count"],
        }
    return candidates, diagnostics

