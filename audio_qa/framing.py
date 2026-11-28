"""Segment-safe framing with explicit incomplete-frame handling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    index: int
    start: int
    end: int
    complete: bool

    @property
    def length(self) -> int:
        return self.end - self.start


def iter_frames(segment_start: int, segment_end: int, frame_length: int, hop: int):
    """Yield frames inside one segment only; never crosses a segment boundary."""
    if frame_length <= 0 or hop <= 0:
        raise ValueError("frame_length and hop must be positive")
    cursor = int(segment_start)
    index = 0
    while cursor < segment_end:
        end = min(cursor + frame_length, segment_end)
        yield Frame(index=index, start=cursor, end=end, complete=(end - cursor) == frame_length)
        cursor += hop
        index += 1


def frame_count(segment_start: int, segment_end: int, hop: int) -> int:
    length = max(0, int(segment_end) - int(segment_start))
    if length == 0:
        return 0
    return 1 + (length - 1) // hop
