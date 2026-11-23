from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .errors import fail
from .hashing import digest_obj


@dataclass(frozen=True)
class NoiseParams:
    octaves: int = 5
    base_frequency: float = 3.0
    lacunarity: float = 2.0
    persistence: float = 0.5
    phase_x: float = 0.0
    phase_y: float = 0.0

    def validate(self) -> None:
        if not 1 <= self.octaves <= 12:
            fail("E_PARAM_INVALID", "noise.octaves 必须在 1..12 之间", field="noise.octaves")
        if not 0.25 <= self.base_frequency <= 128.0:
            fail("E_PARAM_INVALID", "noise.base_frequency 必须在 0.25..128 之间", field="noise.base_frequency")
        if not 1.0 < self.lacunarity <= 4.0:
            fail("E_PARAM_INVALID", "noise.lacunarity 必须在 (1,4] 之间", field="noise.lacunarity")
        if not 0.05 <= self.persistence <= 0.95:
            fail("E_PARAM_INVALID", "noise.persistence 必须在 0.05..0.95 之间", field="noise.persistence")
        for name, value in (("phase_x", self.phase_x), ("phase_y", self.phase_y)):
            if not math.isfinite(value) or abs(value) > 1024:
                fail("E_PARAM_INVALID", f"noise.{name} 必须是绝对值不超过 1024 的有限数", field=f"noise.{name}")

    def octave_weights(self) -> tuple[float, ...]:
        weights = tuple(self.persistence**index for index in range(self.octaves))
        total = sum(weights)
        return tuple(weight / total for weight in weights)

    def digest(self) -> str:
        return digest_obj(asdict(self))


@dataclass(frozen=True)
class MaterialParams:
    material_id: str = "mineral-slate"
    name: str = "Mineral Slate"
    seed: int = 20261124
    revision: int = 1
    base_color: tuple[int, int, int] = (91, 112, 128)
    roughness: float = 0.56
    roughness_contrast: float = 0.30
    normal_strength: float = 2.2
    saturation: float = 1.0
    seam_width: float = 4.0
    seam_strength: float = 0.07
    noise: NoiseParams = field(default_factory=NoiseParams)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.material_id or len(self.material_id) > 64:
            fail("E_PARAM_INVALID", "material_id 必须非空且不超过 64 字符", field="material_id")
        if not isinstance(self.seed, int) or not 0 <= self.seed < 2**63:
            fail("E_PARAM_INVALID", "seed 必须是 0..2^63-1 的整数", field="seed")
        if not isinstance(self.revision, int) or self.revision < 1:
            fail("E_PARAM_INVALID", "revision 必须 >= 1", field="revision")
        if len(self.base_color) != 3 or any(not isinstance(v, int) or not 0 <= v <= 255 for v in self.base_color):
            fail("E_PARAM_INVALID", "base_color 必须是 3 个 0..255 的整数", field="base_color")
        for name, value, low, high in (
            ("roughness", self.roughness, 0.0, 1.0),
            ("roughness_contrast", self.roughness_contrast, 0.0, 1.0),
            ("normal_strength", self.normal_strength, 0.0, 16.0),
            ("saturation", self.saturation, 0.0, 2.0),
            ("seam_width", self.seam_width, 0.5, 64.0),
            ("seam_strength", self.seam_strength, 0.0, 0.95),
        ):
            if not math.isfinite(value) or not low <= value <= high:
                fail("E_PARAM_INVALID", f"{name} 必须在 {low}..{high} 之间", field=name)
        self.noise.validate()

    def digest(self) -> str:
        self.validate()
        return digest_obj(asdict(self))

    def with_revision(self, revision: int, **changes: Any) -> MaterialParams:
        if revision <= self.revision:
            fail(
                "E_REVISION_NOT_MONOTONIC",
                "新参数修订版本必须大于当前版本",
                current_revision=self.revision,
                incoming_revision=revision,
            )
        updated = replace(self, revision=revision, **changes)
        updated.validate()
        return updated

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def changed_leaf_paths(before: MaterialParams, after: MaterialParams) -> list[str]:
    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for key, item in value.items():
                output.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
            return output
        return {prefix: value}

    old = flatten(before.to_dict())
    new = flatten(after.to_dict())
    paths = []
    for key in sorted(old.keys() | new.keys()):
        if old.get(key) != new.get(key):
            paths.append(key)
    return paths

