from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .concurrency import precompute_clip_cache
from .engine import (
    build_chunk_dependencies,
    build_replay,
    chunk_bounds,
    chunk_times,
    compare_replays,
    simulate_frames,
)
from .models import ChunkResult, ReplayResult, Spec
from .schema import EditRecord, LoadedInput, apply_edits, descendants, version_id


@dataclass
class IncrementalReport:
    edit_records: list[EditRecord]
    edit_invalidations: list[dict[str, Any]]
    dirty_chunks: list[int]
    recomputed_chunks: list[int]
    reused_chunks: list[int]
    first_over_tolerance: dict[str, Any] | None = None
    events_equal: bool = True
    max_abs_diff: float = 0.0
    preserved_reused_payloads: bool = True

    def to_dict(self, *, version_before: str, version_after: str) -> dict[str, Any]:
        return {
            "version_before": version_before,
            "version_after": version_after,
            "edit_records": [record.to_dict() for record in self.edit_records],
            "edit_invalidations": self.edit_invalidations,
            "dirty_chunks": self.dirty_chunks,
            "recomputed_chunk_count": len(self.recomputed_chunks),
            "reused_chunk_count": len(self.reused_chunks),
            "recomputed_chunks": self.recomputed_chunks,
            "reused_chunks": self.reused_chunks,
            "preserved_reused_payloads": self.preserved_reused_payloads,
            "first_over_tolerance": self.first_over_tolerance,
            "events_equal": self.events_equal,
            "max_abs_diff": self.max_abs_diff,
        }


def _track_times(spec: Spec, clip_id: str, bone: str, channel: str) -> list[int]:
    track = spec.clips[clip_id].tracks.get(bone, {}).get(channel)
    if track is None:
        return []
    return [key.t for key in track.keyframes]


def _neighbors(times: Sequence[int], t: int) -> tuple[int | None, int | None]:
    previous: int | None = None
    following: int | None = None
    for value in times:
        if value < t:
            previous = value
        elif value > t and following is None:
            following = value
    return previous, following


def _keyframe_ranges(
    spec: Spec,
    *,
    op: str,
    clip_id: str,
    bone: str,
    channel: str,
    t: int,
    new_t: int | None = None,
) -> list[tuple[int, int]]:
    clip = spec.clips[clip_id]
    times = _track_times(spec, clip_id, bone, channel)
    ranges: list[tuple[int, int]] = []

    def add_around(time_value: int) -> None:
        previous, following = _neighbors(times, time_value)
        if previous is None:
            ranges.append((0, following if following is not None else clip.duration_ms))
        elif following is None:
            ranges.append((previous, clip.duration_ms))
        else:
            ranges.append((previous, following))
        if previous is not None:
            ranges.append((previous, time_value))
        if following is not None:
            ranges.append((time_value, following))

    if op == "delete_keyframe":
        add_around(t)
    elif op == "move_keyframe" and new_t is not None:
        add_around(t)
        add_around(new_t)
    else:
        # For set/add, the two adjacent interpolation segments can change.
        add_around(t)
    return sorted({(min(a, b), max(a, b)) for a, b in ranges})


def _overlaps(
    interval: tuple[int, int],
    ranges: Sequence[tuple[int, int]],
) -> bool:
    return any(not (interval[1] < lo or interval[0] > hi) for lo, hi in ranges)


def _frames_for_keyframe_edit(
    base_spec: Spec,
    base_result: ReplayResult,
    *,
    op: str,
    clip_id: str,
    bone: str,
    channel: str,
    t: int,
    new_t: int | None,
) -> tuple[set[int], list[dict[str, Any]]]:
    ranges = _keyframe_ranges(
        base_spec,
        op=op,
        clip_id=clip_id,
        bone=bone,
        channel=channel,
        t=t,
        new_t=new_t,
    )
    affected_chunks: set[int] = set()
    reasons: list[dict[str, Any]] = []
    existing_track = (
        clip_id in base_spec.clips
        and bone in base_spec.clips[clip_id].tracks
        and channel in base_spec.clips[clip_id].tracks.get(bone, {})
    )
    for chunk in base_result.chunks:
        frame_hits: list[int] = []
        for frame in chunk.frames:
            if frame.clip != clip_id:
                continue
            hit = False
            for sample in frame.track_samples:
                if sample.bone == bone and sample.channel == channel:
                    if _overlaps((sample.lo_ms, sample.hi_ms), ranges):
                        hit = True
                    break
            if not hit and (op == "add_keyframe" and not existing_track):
                hit = any(lo <= frame.local_ms <= hi for lo, hi in ranges)
            if hit:
                frame_hits.append(frame.t)
        if frame_hits:
            affected_chunks.add(chunk.index)
            reasons.append(
                {
                    "reason": "keyframe_interval",
                    "chunk": chunk.index,
                    "logical_start_ms": chunk.start_ms,
                    "logical_end_ms": chunk.end_ms,
                    "clip": clip_id,
                    "bone": bone,
                    "channel": channel,
                    "affected_local_ranges": [list(item) for item in ranges],
                    "affected_frames": frame_hits,
                    "subtree": list(descendants(base_spec, bone)),
                }
            )
    return affected_chunks, reasons


def _earliest_chunk_for_clip(base_result: ReplayResult, clip_id: str) -> int | None:
    for chunk in base_result.chunks:
        if any(frame.clip == clip_id for frame in chunk.frames):
            return chunk.index
    return None


def _earliest_chunk_for_sources(
    base_result: ReplayResult,
    sources: Sequence[str],
) -> int | None:
    if "*" in sources:
        return 0
    source_set = set(sources)
    for chunk in base_result.chunks:
        if any(frame.clip in source_set for frame in chunk.frames):
            return chunk.index
    return None


def _state_shift_invalidation(
    base_result: ReplayResult,
    start_chunk: int | None,
    *,
    reason: str,
    edit: Mapping[str, Any],
) -> tuple[set[int], list[dict[str, Any]]]:
    if start_chunk is None:
        return set(), []
    chunks = {chunk.index for chunk in base_result.chunks if chunk.index >= start_chunk}
    last = max(chunk.index for chunk in base_result.chunks)
    return chunks, [
        {
            "reason": reason,
            "chunk_start": start_chunk,
            "chunk_end": last,
            "logical_start_ms": start_chunk * (base_result.chunks[0].end_ms - base_result.chunks[0].start_ms),
            "explanation": "state, loop completion, or transition timeline can diverge here and for all later chunks",
            "edit": dict(edit),
        }
    ]


def compute_invalidation(
    base_loaded: LoadedInput,
    edit_records: Sequence[EditRecord],
    base_result: ReplayResult,
) -> tuple[set[int], list[dict[str, Any]]]:
    dirty: set[int] = set()
    reports: list[dict[str, Any]] = []
    spec = base_loaded.spec
    for record in edit_records:
        details = record.details
        op = record.op
        chunks_for_edit: set[int] = set()
        reasons: list[dict[str, Any]] = []
        affected_bones: list[str] = []
        affected_channels: list[str] = []
        no_op = False

        if op in {"set_keyframe_value", "add_keyframe", "delete_keyframe", "move_keyframe"}:
            clip_id = str(record.target["clip"])
            bone = str(record.target["bone"])
            channel = str(record.target["channel"])
            affected_bones = list(descendants(spec, bone))
            affected_channels = [f"{name}.{channel}" for name in affected_bones]
            t = int(details.get("t", details.get("old_t", 0)))
            if op == "set_keyframe_value":
                no_op = details.get("old_value") == details.get("new_value")
            new_t = int(details["new_t"]) if op == "move_keyframe" else None
            if not no_op:
                chunks_for_edit, reasons = _frames_for_keyframe_edit(
                    spec,
                    base_result,
                    op=op,
                    clip_id=clip_id,
                    bone=bone,
                    channel=channel,
                    t=t,
                    new_t=new_t,
                )

        elif op in {"set_clip_duration", "set_clip_loop_count"}:
            clip_id = str(record.target["clip"])
            no_op = (
                details.get("old_duration_ms") == details.get("new_duration_ms")
                if op == "set_clip_duration"
                else details.get("old_loop_count") == details.get("new_loop_count")
            )
            if not no_op:
                start = _earliest_chunk_for_clip(base_result, clip_id)
                chunks_for_edit, reasons = _state_shift_invalidation(
                    base_result,
                    start,
                    reason="clip_duration_shift" if op == "set_clip_duration" else "clip_loop_count_shift",
                    edit=record.to_dict(),
                )
                if start is not None:
                    affected_bones = [bone.name for bone in spec.skeleton]

        elif op == "set_transition_condition":
            transition_id = str(record.target["transition"])
            transition = next(item for item in spec.transitions if item.id == transition_id)
            no_op = (
                details.get("old_when") == details.get("new_when")
                and details.get("old_on_finish") == details.get("new_on_finish")
            )
            if not no_op:
                start = _earliest_chunk_for_sources(base_result, transition.sources)
                chunks_for_edit, reasons = _state_shift_invalidation(
                    base_result,
                    start,
                    reason="transition_condition_shift",
                    edit=record.to_dict(),
                )

        elif op == "add_transition":
            transition = details["transition"]
            sources = transition.get("from", [])
            if isinstance(sources, str):
                sources = [sources]
            start = _earliest_chunk_for_sources(base_result, sources)
            chunks_for_edit, reasons = _state_shift_invalidation(
                base_result,
                start,
                reason="transition_added_shift",
                edit=record.to_dict(),
            )

        elif op == "delete_transition":
            transition_id = str(record.target["transition"])
            transition = next(item for item in spec.transitions if item.id == transition_id)
            start = _earliest_chunk_for_sources(base_result, transition.sources)
            chunks_for_edit, reasons = _state_shift_invalidation(
                base_result,
                start,
                reason="transition_deleted_shift",
                edit=record.to_dict(),
            )

        dirty.update(chunks_for_edit)
        reports.append(
            {
                "edit_index": record.index,
                "op": op,
                "target": record.target,
                "no_op": no_op,
                "affected_bones": affected_bones,
                "affected_channels": affected_channels,
                "invalidated_chunks": sorted(chunks_for_edit),
                "reasons": reasons,
            }
        )
    return dirty, reports


def _contiguous_runs(indices: Iterable[int]) -> list[list[int]]:
    values = sorted(set(indices))
    if not values:
        return []
    runs: list[list[int]] = [[values[0]]]
    for value in values[1:]:
        if value == runs[-1][-1] + 1:
            runs[-1].append(value)
        else:
            runs.append([value])
    return runs


def incremental_replay(
    base_loaded: LoadedInput,
    edit_script: Mapping[str, Any],
    *,
    base_result: ReplayResult | None = None,
    reference: ReplayResult | None = None,
    max_workers: int = 1,
    memory_budget_bytes: int = 64 * 1024 * 1024,
    tolerance: float = 1e-6,
) -> tuple[ReplayResult, IncrementalReport]:
    edited_loaded, edit_records = apply_edits(base_loaded, edit_script)
    baseline = base_result or build_replay(
        base_loaded.spec,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )
    dirty, invalidation_reports = compute_invalidation(base_loaded, edit_records, baseline)
    cache, concurrency_report = precompute_clip_cache(
        edited_loaded.spec,
        max_workers=max_workers,
        memory_budget_bytes=memory_budget_bytes,
    )

    rebuilt: dict[int, ChunkResult] = {}
    reused: list[int] = []
    recomputed: list[int] = []
    preserved = True
    for run in _contiguous_runs(dirty):
        state = baseline.chunks[run[0]].state_at_start
        for index in run:
            spec = edited_loaded.spec
            times = chunk_times(spec, index)
            start_ms, end_ms = chunk_bounds(spec, index)
            state_at_start = state
            slice_result = simulate_frames(spec, cache, times, state)
            state = slice_result.state_at_end
            rebuilt[index] = ChunkResult(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                frames=slice_result.frames,
                events=slice_result.events,
                state_at_start=state_at_start,
                state_at_end=state,
                dependencies=build_chunk_dependencies(
                    spec,
                    slice_result.frames,
                    slice_result.events,
                    start_ms,
                    end_ms,
                ),
                source="recomputed",
            )
            recomputed.append(index)

    chunks: list[ChunkResult] = []
    baseline_by_index = {chunk.index: chunk for chunk in baseline.chunks}
    for index in sorted(baseline_by_index):
        if index in rebuilt:
            chunk = rebuilt[index]
        else:
            baseline_chunk = baseline_by_index[index]
            chunk = ChunkResult(
                index=baseline_chunk.index,
                start_ms=baseline_chunk.start_ms,
                end_ms=baseline_chunk.end_ms,
                frames=baseline_chunk.frames,
                events=baseline_chunk.events,
                state_at_start=baseline_chunk.state_at_start,
                state_at_end=baseline_chunk.state_at_end,
                dependencies=baseline_chunk.dependencies,
                source="reused",
            )
            reused.append(index)
            if chunk.payload_digest() != baseline_chunk.payload_digest():
                preserved = False
        chunks.append(chunk)

    result = ReplayResult(
        spec=edited_loaded.spec,
        version_id=version_id(edited_loaded.spec),
        chunks=chunks,
        concurrency=concurrency_report,
    )
    report = IncrementalReport(
        edit_records=edit_records,
        edit_invalidations=invalidation_reports,
        dirty_chunks=sorted(dirty),
        recomputed_chunks=sorted(recomputed),
        reused_chunks=sorted(reused),
        preserved_reused_payloads=preserved,
    )
    if reference is not None:
        comparison = compare_replays(reference, result, tolerance=tolerance)
        report.first_over_tolerance = comparison["first_over_tolerance"]
        report.events_equal = comparison["events_equal"]
        report.max_abs_diff = comparison["max_abs_diff"]
        report.preserved_reused_payloads = preserved and comparison["ok"]
    return result, report

