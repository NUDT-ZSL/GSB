"""帧规划：从时间轴生成确定的帧序列。

帧 i 的时间 = round(i * 1000 / fps)，帧数 = round(total_ms * fps / 1000)，
因此时长严格等于素材总长度（误差 < 半帧）。每帧记录所在片段、镜头编号、
片段边界与应显示的字幕文本，渲染与报告共用这份规划，保证所见即所报。
"""

from __future__ import annotations

from .models import FrameSpec, PlacedShot
from .subtitles import SubtitleIndex
from .timeline import Gap, SegmentMap

KIND_SHOT = "shot"
KIND_GAP = "gap"

PALETTE_SIZE = 6


def frame_count(total_ms: int, fps: int) -> int:
    return (total_ms * fps * 2 + 1000) // 2000  # round-half-up，避免浮点误差


def frame_time_ms(frame_index: int, fps: int) -> int:
    return (frame_index * 2000 + fps) // (2 * fps)  # round-half-up


def plan_frames(
    placed: list[PlacedShot],
    gaps: list[Gap],
    total_ms: int,
    fps: int,
    subtitle_index: SubtitleIndex,
    shot_colors: dict[str, int],
) -> list[FrameSpec]:
    count = frame_count(total_ms, fps)
    segments = SegmentMap(placed, gaps)
    frames: list[FrameSpec] = []
    for index in range(count):
        time_ms = frame_time_ms(index, fps)
        seg = segments.at(time_ms)
        texts = subtitle_index.texts_at(time_ms)
        if isinstance(seg, Gap):
            frames.append(
                FrameSpec(
                    index=index,
                    time_ms=time_ms,
                    kind=KIND_GAP,
                    shot_id=None,
                    segment_start_ms=seg.start_ms,
                    segment_end_ms=seg.end_ms,
                    palette_index=-1,
                    subtitle_texts=texts,
                )
            )
        elif isinstance(seg, PlacedShot):
            frames.append(
                FrameSpec(
                    index=index,
                    time_ms=time_ms,
                    kind=KIND_SHOT,
                    shot_id=seg.shot.shot_id,
                    segment_start_ms=seg.effective_start_ms,
                    segment_end_ms=seg.effective_end_ms,
                    palette_index=shot_colors.get(seg.shot.shot_id, 0),
                    subtitle_texts=texts,
                )
            )
        else:  # 理论上不会发生：帧时间必然落在某个片段内
            frames.append(
                FrameSpec(
                    index=index,
                    time_ms=time_ms,
                    kind=KIND_GAP,
                    shot_id=None,
                    segment_start_ms=time_ms,
                    segment_end_ms=time_ms + 1,
                    palette_index=-1,
                    subtitle_texts=texts,
                )
            )
    return frames


def gap_frames(frames: list[FrameSpec]) -> list[int]:
    return [f.index for f in frames if f.kind == KIND_GAP]
