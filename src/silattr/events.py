"""事件时序合并、时间轴校验与通话级静音画像。"""

from .config import CATEGORY_DEVICE
from .errors import DomainError, E_RANGE_OUT, E_OVERLAP, E_INCONSISTENT
from .models import Event


def merge_events(attributions, silence, step_positions, merge_gap_frames,
                 n_frames, call_id=""):
    """相邻且同归因、间隔不超过 merge_gap_frames 的静音段合并为事件。"""
    by_id = {segment.silence_id: segment for segment in silence}
    ordered = sorted(attributions, key=lambda item: item.start_frame)
    groups = []
    for item in ordered:
        if groups:
            previous = groups[-1][-1]
            gap = item.start_frame - previous.end_frame
            if item.category == previous.category and 0 <= gap <= merge_gap_frames:
                groups[-1].append(item)
                continue
        groups.append([item])

    events = []
    for event_id, group in enumerate(groups):
        start = group[0].start_frame
        end = group[-1].end_frame
        category = group[0].category
        silence_ids = [item.silence_id for item in group]
        step_count = 0
        if category == CATEGORY_DEVICE:
            step_count = sum(
                1 for position in step_positions if start <= position < end
            )
        events.append(
            Event(
                event_id=event_id,
                start_frame=start,
                end_frame=end,
                category=category,
                category_name=group[0].category_name,
                silence_ids=silence_ids,
                step_count=step_count,
            )
        )

    validate_timeline(events, n_frames, call_id)
    return events


def validate_timeline(events, n_frames, call_id=""):
    """事件须按时间升序、互不重叠且落在通话时长内。"""
    previous_end = 0
    for event in events:
        if (
            event.start_frame < 0
            or event.end_frame > n_frames
            or event.start_frame >= event.end_frame
        ):
            raise DomainError(
                E_RANGE_OUT,
                "事件越出通话时长或帧区间非法",
                call_id=call_id,
                event_id=event.event_id,
                start_frame=event.start_frame,
                end_frame=event.end_frame,
                n_frames=n_frames,
            )
        if event.start_frame < previous_end:
            raise DomainError(
                E_OVERLAP,
                "事件时间轴存在重叠或未按升序排列",
                call_id=call_id,
                event_id=event.event_id,
                previous_end=previous_end,
                start_frame=event.start_frame,
            )
        previous_end = event.end_frame


def build_profile(events, frame_ms, categories):
    """通话级画像：各类别总时长、事件数、突变次数。"""
    profile = {}
    for category in categories:
        profile[str(category)] = {
            "total_ms": 0,
            "event_count": 0,
            "step_count": 0,
        }
    for event in events:
        bucket = profile[str(event.category)]
        bucket["total_ms"] += (event.end_frame - event.start_frame) * frame_ms
        bucket["event_count"] += 1
        bucket["step_count"] += event.step_count
    return profile


def recompute_profile(events, frame_ms, categories):
    """仅依据逐事件明细独立重算画像。"""
    profile = {
        str(category): {"total_ms": 0, "event_count": 0, "step_count": 0}
        for category in categories
    }
    for event in events:
        bucket = profile[str(event.category)]
        bucket["total_ms"] += (event.end_frame - event.start_frame) * frame_ms
        bucket["event_count"] += 1
        bucket["step_count"] += event.step_count
    return profile


def assert_profile_consistent(events, profile, frame_ms, categories, call_id=""):
    independent = recompute_profile(events, frame_ms, categories)
    if independent != profile:
        raise DomainError(
            E_INCONSISTENT,
            "画像无法由逐事件明细独立重算一致",
            call_id=call_id,
            stored=profile,
            recomputed=independent,
        )
    return independent

