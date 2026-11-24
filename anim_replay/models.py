from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .math3d import Quat, Transform, Vec3

CHANNELS = ("translation", "rotation", "scale")


@dataclass(frozen=True)
class Bone:
    name: str
    parent: str | None
    rest: Transform


@dataclass(frozen=True)
class Keyframe:
    t: int
    value: tuple[float, ...]


@dataclass(frozen=True)
class Track:
    bone: str
    channel: str
    keyframes: tuple[Keyframe, ...]


@dataclass(frozen=True)
class Clip:
    id: str
    duration_ms: int
    loop_count: int
    tracks: Mapping[str, Mapping[str, Track]]


@dataclass(frozen=True)
class Condition:
    param: str
    op: str
    value: Any


@dataclass(frozen=True)
class Transition:
    id: str
    sources: tuple[str, ...]
    target: str
    when: tuple[Condition, ...]
    priority: int
    interrupt: bool
    loop_count: int | None
    min_hold_ms: int
    on_finish: bool


@dataclass(frozen=True)
class Parameter:
    name: str
    default: Any
    track: tuple[tuple[int, Any], ...]


@dataclass(frozen=True)
class Timeline:
    duration_ms: int
    frame_step_ms: int
    chunk_ms: int

    @property
    def times(self) -> tuple[int, ...]:
        return tuple(range(0, self.duration_ms + 1, self.frame_step_ms))


@dataclass(frozen=True)
class Spec:
    seed: int | None
    skeleton: tuple[Bone, ...]
    bone_order: tuple[str, ...]
    clips: Mapping[str, Clip]
    parameters: Mapping[str, Parameter]
    transitions: tuple[Transition, ...]
    initial_clip: str
    timeline: Timeline
    source_digest: str


@dataclass(frozen=True)
class TrackSample:
    bone: str
    channel: str
    lo_ms: int
    hi_ms: int


@dataclass(frozen=True)
class MachineState:
    active_clip: str
    enter_ms: int
    target_loop_count: int
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_clip": self.active_clip,
            "enter_ms": self.enter_ms,
            "target_loop_count": self.target_loop_count,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MachineState":
        return cls(
            active_clip=str(value["active_clip"]),
            enter_ms=int(value["enter_ms"]),
            target_loop_count=int(value["target_loop_count"]),
            generation=int(value["generation"]),
        )


@dataclass(frozen=True)
class ReplayFrame:
    t: int
    clip: str
    local_ms: int
    generation: int
    world: Mapping[str, Transform]
    track_samples: tuple[TrackSample, ...]

    def bones_flat(self) -> dict[str, list[float]]:
        result: dict[str, list[float]] = {}
        for bone, (translation, rotation, scale) in self.world.items():
            result[f"{bone}.translation"] = list(translation)
            result[f"{bone}.rotation"] = list(rotation)
            result[f"{bone}.scale"] = list(scale)
        return result

    def to_dict(self, *, include_dependencies: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "t": self.t,
            "clip": self.clip,
            "local_ms": self.local_ms,
            "state_generation": self.generation,
            "bones": self.bones_flat(),
        }
        if include_dependencies:
            value["dependencies"] = {
                "tracks": [
                    {
                        "bone": item.bone,
                        "channel": item.channel,
                        "lo_ms": item.lo_ms,
                        "hi_ms": item.hi_ms,
                    }
                    for item in self.track_samples
                ]
            }
        return value


@dataclass(frozen=True)
class TransitionEvent:
    t: int
    rule_id: str
    source_clip: str
    target_clip: str
    priority: int
    state_generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "transition",
            "t": self.t,
            "rule_id": self.rule_id,
            "from": self.source_clip,
            "to": self.target_clip,
            "priority": self.priority,
            "state_generation": self.state_generation,
        }


@dataclass
class ChunkResult:
    index: int
    start_ms: int
    end_ms: int
    frames: list[ReplayFrame]
    events: list[TransitionEvent]
    state_at_start: MachineState
    state_at_end: MachineState
    dependencies: dict[str, Any] = field(default_factory=dict)
    source: str = "computed"

    def content_dict(self, version_id: str) -> dict[str, Any]:
        return {
            "format": "anim-replay-chunk-v1",
            "version_id": version_id,
            "chunk_index": self.index,
            "logical_start_ms": self.start_ms,
            "logical_end_ms": self.end_ms,
            "state_at_start": self.state_at_start.to_dict(),
            "state_at_end": self.state_at_end.to_dict(),
            "frame_count": len(self.frames),
            "frames": [frame.to_dict(include_dependencies=True) for frame in self.frames],
            "events": [event.to_dict() for event in self.events],
            "dependencies": self.dependencies,
        }

    def payload_digest(self) -> str:
        from .canonical import sha256_value

        return sha256_value(
            {
                "frames": [frame.to_dict(include_dependencies=True) for frame in self.frames],
                "events": [event.to_dict() for event in self.events],
                "dependencies": self.dependencies,
            }
        )


@dataclass
class ReplayResult:
    spec: Spec
    version_id: str
    chunks: list[ChunkResult]
    concurrency: dict[str, Any] = field(default_factory=dict)

    @property
    def frames(self) -> list[ReplayFrame]:
        return [frame for chunk in self.chunks for frame in chunk.frames]

    @property
    def events(self) -> list[TransitionEvent]:
        return [event for chunk in self.chunks for event in chunk.events]

