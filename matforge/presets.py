from __future__ import annotations

from dataclasses import replace

from .errors import fail
from .params import MaterialParams, NoiseParams

_PRESETS: dict[str, MaterialParams] = {
    "mineral-slate": MaterialParams(),
    "oxidized-iron": MaterialParams(
        material_id="oxidized-iron",
        name="Oxidized Iron",
        seed=771904,
        base_color=(142, 83, 54),
        roughness=0.68,
        roughness_contrast=0.42,
        normal_strength=3.1,
        saturation=1.08,
        seam_width=3.5,
        seam_strength=0.06,
        noise=NoiseParams(octaves=6, base_frequency=4.5, lacunarity=2.15, persistence=0.57, phase_x=0.37),
        metadata={"family": "metal-oxide", "finish": "rough"},
    ),
    "desert-sandstone": MaterialParams(
        material_id="desert-sandstone",
        name="Desert Sandstone",
        seed=440021,
        base_color=(173, 139, 91),
        roughness=0.74,
        roughness_contrast=0.26,
        normal_strength=1.5,
        saturation=0.92,
        seam_width=5.5,
        seam_strength=0.05,
        noise=NoiseParams(octaves=4, base_frequency=2.2, lacunarity=2.4, persistence=0.45, phase_y=0.61),
        metadata={"family": "sedimentary", "finish": "matte"},
    ),
}


def preset_names() -> list[str]:
    return sorted(_PRESETS)


def get_preset(name: str) -> MaterialParams:
    preset = _PRESETS.get(name)
    if preset is None:
        fail(
            "E_PRESET_NOT_FOUND",
            "材质预设不存在",
            preset=name,
            available=preset_names(),
        )
    return replace(preset, metadata=dict(preset.metadata))


def preset_summaries() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "material_id": preset.material_id,
            "seed": preset.seed,
            "base_color": list(preset.base_color),
            "octaves": preset.noise.octaves,
        }
        for name, preset in sorted(_PRESETS.items())
    ]

