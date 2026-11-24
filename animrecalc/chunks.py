from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ChunkBounds:
    index: int
    start_frame: int
    end_frame: int
    frame_count: int
    start_ms: int
    end_ms: int

    def to_dict(self) -> dict[str, int]:
        return {
            "chunk_index": self.index,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "frame_count": self.frame_count,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
        }


class ChunkLayout:
    def __init__(self, spec: dict[str, Any]):
        self.step_ms = int(spec["step_ms"])
        self.frames_per_chunk = int(spec["chunk_frames"])
        timeline_ms = int(spec["timeline_ms"])
        if timeline_ms % self.step_ms != 0:
            # Integer logical frames still work with floor, but samples and reports
            # are clearer when the horizon lands on a step boundary.
            self.total_frames = timeline_ms // self.step_ms + 1
            self.timeline_ms = (self.total_frames - 1) * self.step_ms
        else:
            self.total_frames = timeline_ms // self.step_ms + 1
            self.timeline_ms = timeline_ms
        self.chunk_count = math.ceil(self.total_frames / self.frames_per_chunk)

    def bounds(self, index: int) -> ChunkBounds:
        if index < 0 or index >= self.chunk_count:
            raise IndexError(f"chunk {index} out of range 0..{self.chunk_count - 1}")
        start = index * self.frames_per_chunk
        end = min(self.total_frames, start + self.frames_per_chunk) - 1
        return ChunkBounds(
            index=index,
            start_frame=start,
            end_frame=end,
            frame_count=end - start + 1,
            start_ms=start * self.step_ms,
            end_ms=end * self.step_ms,
        )

    def all_bounds(self) -> list[ChunkBounds]:
        return [self.bounds(i) for i in range(self.chunk_count)]

    def chunk_index_for_frame(self, frame_index: int) -> int:
        return frame_index // self.frames_per_chunk

    def chunk_index_for_ms(self, time_ms: int) -> int:
        return self.chunk_index_for_frame(time_ms // self.step_ms)

    def estimate_chunk_bytes(self, bone_count: int) -> int:
        # Pose matrices plus temporary vector/quaternion interpolation buffers.
        frames = self.frames_per_chunk
        pose_bytes = frames * bone_count * 16 * 8
        scratch_bytes = frames * bone_count * 8 * 16
        return pose_bytes + scratch_bytes
