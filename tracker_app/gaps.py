from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .errors import TrackingError
from .models import Track


@dataclass
class GapCompletionResult:
    rows: list[dict[str, Any]]
    reports: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    degraded_categories: list[str]
    skipped_gaps: list[dict[str, Any]]


def _bbox_from_velocity(
    observations: list[dict[str, Any]],
    frame_index: int,
    canvas: tuple[float, float],
    fallback_velocity: tuple[float, float, float, float],
) -> list[float]:
    ordered = sorted(observations, key=lambda item: item["frame_index"])
    last = ordered[-1]
    if len(ordered) >= 2:
        previous = ordered[-2]
        delta = max(1, int(last["frame_index"]) - int(previous["frame_index"]))
        last_center = (
            (last["bbox"][0] + last["bbox"][2]) / 2.0,
            (last["bbox"][1] + last["bbox"][3]) / 2.0,
        )
        previous_center = (
            (previous["bbox"][0] + previous["bbox"][2]) / 2.0,
            (previous["bbox"][1] + previous["bbox"][3]) / 2.0,
        )
        vx = (last_center[0] - previous_center[0]) / delta
        vy = (last_center[1] - previous_center[1]) / delta
        vw = (
            (last["bbox"][2] - last["bbox"][0])
            - (previous["bbox"][2] - previous["bbox"][0])
        ) / delta
        vh = (
            (last["bbox"][3] - last["bbox"][1])
            - (previous["bbox"][3] - previous["bbox"][1])
        ) / delta
    else:
        vx, vy, vw, vh = fallback_velocity

    step = frame_index - int(last["frame_index"])
    x1 = last["bbox"][0] + vx * step
    y1 = last["bbox"][1] + vy * step
    x2 = last["bbox"][2] + vx * step + vw * step
    y2 = last["bbox"][3] + vy * step + vh * step
    x1 = max(0.0, min(canvas[0] - 2.0, x1))
    y1 = max(0.0, min(canvas[1] - 2.0, y1))
    x2 = max(x1 + 2.0, min(canvas[0], x2))
    y2 = max(y1 + 2.0, min(canvas[1], y2))
    return [round(x1, 6), round(y1, 6), round(x2, 6), round(y2, 6)]


def _complete_track(
    *,
    seq_id: str,
    revision: int,
    track_id: int,
    observations: list[dict[str, Any]],
    track: Track,
    config: dict[str, Any],
    canvas: tuple[float, float],
    manual_intervals: list[dict[str, Any]],
    min_frame: int,
    max_frame: int,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    max_gap = int(config["gap"]["max_gap_frames"])
    real_hit_frames = {int(item["frame_index"]) for item in observations}
    observations = sorted(observations, key=lambda item: item["frame_index"])
    auto_intervals: list[tuple[int, int]] = []
    skipped: list[dict[str, Any]] = []
    for previous, current in zip(observations, observations[1:]):
        start = int(previous["frame_index"]) + 1
        end = int(current["frame_index"]) - 1
        if start > end:
            continue
        gap_length = end - start + 1
        if gap_length > max_gap:
            skipped.append(
                {
                    "seq_id": seq_id,
                    "revision": revision,
                    "track_id": track_id,
                    "gap_start": start,
                    "gap_end": end,
                    "reason": "max_gap_frames_exceeded",
                    "gap_frames": gap_length,
                    "max_gap_frames": max_gap,
                }
            )
            continue
        auto_intervals.append((start, end))

    all_intervals = auto_intervals + [
        (int(item["gap_start"]), int(item["gap_end"])) for item in manual_intervals
    ]
    all_intervals.sort()
    for (first_start, first_end), (second_start, second_end) in zip(
        all_intervals, all_intervals[1:]
    ):
        if second_start <= first_end:
            raise TrackingError(
                "E_GAP_INVALID",
                "gap completion intervals overlap",
                seq_id=seq_id,
                frame_index=second_start,
                category="overlapping_intervals",
                context={
                    "track_id": track_id,
                    "first_interval": [first_start, first_end],
                    "second_interval": [second_start, second_end],
                },
            )
    for start, end in all_intervals:
        if start < min_frame or end > max_frame or start > end:
            raise TrackingError(
                "E_GAP_INVALID",
                "gap completion interval is outside the sequence",
                seq_id=seq_id,
                frame_index=max(start, min_frame),
                category="interval_out_of_bounds",
                context={"track_id": track_id, "interval": [start, end]},
            )
        if end - start + 1 > max_gap:
            raise TrackingError(
                "E_GAP_INVALID",
                "gap completion interval exceeds max_gap_frames",
                seq_id=seq_id,
                frame_index=start,
                category="max_gap_exceeded",
                context={
                    "track_id": track_id,
                    "interval": [start, end],
                    "max_gap_frames": max_gap,
                },
            )
        covered_hits = sorted(
            frame for frame in real_hit_frames if start <= frame <= end
        )
        if covered_hits:
            raise TrackingError(
                "E_GAP_INVALID",
                "gap completion would overwrite a real detection hit",
                seq_id=seq_id,
                frame_index=covered_hits[0],
                category="real_hit_overlap",
                context={"track_id": track_id, "hit_frames": covered_hits},
            )

    rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for start, end in all_intervals:
        before = [item for item in observations if int(item["frame_index"]) < start]
        after = [item for item in observations if int(item["frame_index"]) > end]
        if not before:
            raise TrackingError(
                "E_GAP_INVALID",
                "gap completion requires a real hit before the interval",
                seq_id=seq_id,
                frame_index=start,
                category="missing_anchor_before",
                context={"track_id": track_id},
            )
        source_frame = int(before[-1]["frame_index"])
        target_frame = int(after[0]["frame_index"]) if after else None
        source_truth_ids = [
            int(item["track_id_true"])
            for item in observations
            if int(item["frame_index"]) <= source_frame
        ]
        dominant_truth = max(
            sorted(set(source_truth_ids)),
            key=lambda value: (
                source_truth_ids.count(value),
                -value,
            ),
        )
        completed_frames = []
        for frame_index in range(start, end + 1):
            bbox = _bbox_from_velocity(
                [item for item in observations if int(item["frame_index"]) < frame_index],
                frame_index,
                canvas,
                track.velocity,
            )
            score = round(
                max(
                    0.05,
                    float(observations[-1]["score"])
                    * (0.92 ** (frame_index - source_frame)),
                ),
                6,
            )
            completed_frames.append(frame_index)
            rows.append(
                {
                    "seq_id": seq_id,
                    "revision": revision,
                    "frame_index": frame_index,
                    "detection_index": None,
                    "track_id_true": dominant_truth,
                    "pred_track_id": track_id,
                    "outcome": "completed",
                    "bbox": bbox,
                    "score": score,
                    "class_id": int(observations[-1]["class_id"]),
                    "occluded": True,
                    "iou": None,
                    "appearance_similarity": None,
                    "cost": None,
                    "track_state_after": "lost",
                    "track_event": "gap_filled",
                    "is_completion": True,
                    "completion_source_frame": source_frame,
                    "completion_method": "linear_velocity_extrapolation",
                }
            )
        reports.append(
            {
                "seq_id": seq_id,
                "revision": revision,
                "track_id": track_id,
                "gap_start": start,
                "gap_end": end,
                "gap_frames": end - start + 1,
                "source_frame": source_frame,
                "target_frame": target_frame,
                "completed_frames": completed_frames,
                "method": "linear_velocity_extrapolation",
                "real_hits_inside": 0,
                "completed_bboxes": [row["bbox"] for row in rows if row["frame_index"] in completed_frames],
            }
        )

    segments = _segments_for_track(
        seq_id=seq_id,
        revision=revision,
        track_id=track_id,
        observations=observations,
        completions=rows,
        track=track,
    )
    return rows, reports, segments, skipped


def _contiguous_ranges(frames: list[int]) -> list[list[int]]:
    if not frames:
        return []
    ordered = sorted(set(frames))
    ranges = [[ordered[0], ordered[0]]]
    for frame in ordered[1:]:
        if frame == ranges[-1][1] + 1:
            ranges[-1][1] = frame
        else:
            ranges.append([frame, frame])
    return ranges


def _segments_for_track(
    *,
    seq_id: str,
    revision: int,
    track_id: int,
    observations: list[dict[str, Any]],
    completions: list[dict[str, Any]],
    track: Track,
) -> list[dict[str, Any]]:
    real_frames = sorted({int(item["frame_index"]) for item in observations})
    completed_frames = sorted({int(item["frame_index"]) for item in completions})
    all_frames = sorted(set(real_frames) | set(completed_frames))
    raw_fragments = _contiguous_ranges(real_frames)
    merged_segments = _contiguous_ranges(all_frames)
    truth_counts: dict[int, int] = {}
    for item in observations:
        truth_id = int(item["track_id_true"])
        truth_counts[truth_id] = truth_counts.get(truth_id, 0) + 1
    truth_ids = sorted(truth_counts)
    primary_truth = (
        max(truth_ids, key=lambda value: (truth_counts[value], -value))
        if truth_ids
        else None
    )
    return [
        {
            "seq_id": seq_id,
            "revision": revision,
            "track_id": track_id,
            "primary_truth_track_id": primary_truth,
            "truth_track_ids": truth_ids,
            "birth_frame": track.birth_frame,
            "termination_frame": track.termination_frame,
            "real_hits": len(real_frames),
            "completed_hits": len(completed_frames),
            "raw_fragments": [
                {"start_frame": start, "end_frame": end, "frames": end - start + 1}
                for start, end in raw_fragments
            ],
            "segments": [
                {"start_frame": start, "end_frame": end, "frames": end - start + 1}
                for start, end in merged_segments
            ],
            "merged": len(merged_segments) < len(raw_fragments),
            "fragment_count_before_completion": len(raw_fragments),
            "fragment_count_after_completion": len(merged_segments),
        }
    ]


def complete_gaps(
    *,
    seq_id: str,
    revision: int,
    assignments: list[dict[str, Any]],
    final_tracks: dict[int, Track],
    config: dict[str, Any],
    frame_range: tuple[int, int],
) -> GapCompletionResult:
    canvas = (
        float(config["generator"]["canvas"][0]),
        float(config["generator"]["canvas"][1]),
    )
    observations_by_track: dict[int, list[dict[str, Any]]] = {}
    for row in assignments:
        if row.get("pred_track_id") is None:
            continue
        observations_by_track.setdefault(int(row["pred_track_id"]), []).append(row)

    manual_by_track: dict[int, list[dict[str, Any]]] = {}
    for plan in config["gap"].get("manual_completion_plans", []):
        plan_seq = plan.get("seq_id")
        if plan_seq is not None and plan_seq != seq_id:
            continue
        track_id = int(plan["track_id"])
        if track_id not in final_tracks:
            raise TrackingError(
                "E_GAP_INVALID",
                "manual completion plan references an unknown track",
                seq_id=seq_id,
                frame_index=int(plan["gap_start"]),
                category="unknown_track",
                context={"track_id": track_id},
            )
        manual_by_track.setdefault(track_id, []).append(plan)

    jobs: list[tuple[int, list[dict[str, Any]]]] = []
    for track_id in sorted(final_tracks):
        observations = observations_by_track.get(track_id, [])
        jobs.append((track_id, observations))

    max_parallel = int(config["resources"]["max_parallel_gap_tracks"])
    policy = str(config["resources"]["policy"])
    degraded: set[str] = set()
    if len(jobs) > max_parallel:
        if policy == "error":
            raise TrackingError(
                "E_RESOURCE_LIMIT",
                "parallel gap completion track limit exceeded",
                seq_id=seq_id,
                category="parallel_gap_tracks",
                context={"requested": len(jobs), "max_parallel": max_parallel},
            )
        degraded.add("gap_parallelism_capped")

    def run_job(
        job: tuple[int, list[dict[str, Any]]]
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        track_id, observations = job
        rows, reports, segments, skipped = _complete_track(
            seq_id=seq_id,
            revision=revision,
            track_id=track_id,
            observations=observations,
            track=final_tracks[track_id],
            config=config,
            canvas=canvas,
            manual_intervals=manual_by_track.get(track_id, []),
            min_frame=frame_range[0],
            max_frame=frame_range[1],
        )
        return rows, reports, segments, skipped

    all_rows: list[dict[str, Any]] = []
    all_reports: list[dict[str, Any]] = []
    all_segments: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if config["gap"].get("parallel_gap_completion", True):
        for offset in range(0, len(jobs), max_parallel):
            batch = jobs[offset : offset + max_parallel]
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(batch))) as executor:
                results = list(executor.map(run_job, batch))
            for rows, reports, segments, job_skipped in results:
                all_rows.extend(rows)
                all_reports.extend(reports)
                all_segments.extend(segments)
                skipped.extend(job_skipped)
    else:
        for job in jobs:
            rows, reports, segments, job_skipped = run_job(job)
            all_rows.extend(rows)
            all_reports.extend(reports)
            all_segments.extend(segments)
            skipped.extend(job_skipped)

    all_rows.sort(key=lambda row: (row["frame_index"], row["pred_track_id"]))
    all_reports.sort(key=lambda row: (row["track_id"], row["gap_start"]))
    all_segments.sort(key=lambda row: row["track_id"])
    skipped.sort(key=lambda row: (row["track_id"], row["gap_start"]))
    return GapCompletionResult(
        rows=all_rows,
        reports=all_reports,
        segments=all_segments,
        degraded_categories=sorted(degraded),
        skipped_gaps=skipped,
    )
