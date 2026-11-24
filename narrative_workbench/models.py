from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class FixedKey:
    id: str
    label: str
    minimum: int
    maximum: int
    initial: int
    initial_text: str


@dataclass(frozen=True)
class BoolKey:
    id: str
    label: str
    initial: bool


StateKey = FixedKey | BoolKey


@dataclass(frozen=True)
class StateSchema:
    keys: dict[str, StateKey]
    fixed_scale: int
    residual_tolerance: Decimal
    residual_tolerance_text: str


@dataclass(frozen=True)
class Condition:
    key: str
    op: str
    value: int | bool
    threshold_text: str


@dataclass(frozen=True)
class Effect:
    key: str
    op: str
    value: int | bool
    value_text: str


@dataclass(frozen=True)
class EventDef:
    id: str
    priority: int
    preconditions: tuple[Condition, ...]
    effects: tuple[Effect, ...]
    declaration_index: int


@dataclass(frozen=True)
class BranchDef:
    id: str
    preconditions: tuple[Condition, ...]
    effects: tuple[Effect, ...]
    next_scene: str | None
    is_default: bool
    declaration_index: int


@dataclass(frozen=True)
class SceneDef:
    id: str
    title: str
    start_time: int
    events: tuple[EventDef, ...]
    branches: tuple[BranchDef, ...]
    declaration_index: int


@dataclass(frozen=True)
class Story:
    version_id: str
    start_scene: str
    scenes: dict[str, SceneDef]


@dataclass(frozen=True)
class Layer:
    kind: str
    rect: tuple[int, int, int, int]
    gray: int
    text: str
    scale: int


@dataclass(frozen=True)
class SceneVisual:
    scene_id: str
    template: tuple[Layer, ...]
    markers: dict[str, tuple[int, int, int, int]]


@dataclass(frozen=True)
class Storyboard:
    width: int
    height: int
    background: int
    scenes: dict[str, SceneVisual]


@dataclass(frozen=True)
class RunConfig:
    choices: dict[str, str]
    max_steps: int = 20000


@dataclass(frozen=True)
class Bundle:
    state: StateSchema
    story: Story
    storyboard: Storyboard
    run: RunConfig
    version_id: str
    run_id: str
    source_digests: dict[str, str]

    def event_by_id(self) -> dict[str, EventDef]:
        return {
            event.id: event
            for scene in self.story.scenes.values()
            for event in scene.events
        }


def layer_to_dict(layer: Layer) -> dict[str, Any]:
    return {
        "kind": layer.kind,
        "rect": list(layer.rect),
        "gray": layer.gray,
        "text": layer.text,
        "scale": layer.scale,
    }

