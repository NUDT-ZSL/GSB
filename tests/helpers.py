"""测试用最小合法材质与配置构造器。"""

from __future__ import annotations

from matgen.model import ChannelSpec, GenConfig, MaterialSpec


def make_config(**overrides) -> GenConfig:
    base = dict(
        grid_rows=2,
        grid_cols=2,
        concurrency=2,
        memory_budget_bytes=4 * 1024 * 1024,
        overlap=2,
        tolerance=1e-6,
        output_dir="build/output",
        state_dir="build/state",
        archive_dir="build/archive",
        dtype="float32",
    )
    base.update(overrides)
    return GenConfig(**base)


def make_spec(material_id: str = "mat_test", **overrides) -> MaterialSpec:
    base = dict(
        material_id=material_id,
        seed=7,
        width=32,
        height=24,
        base_color=(0.4, 0.5, 0.6),
        roughness=0.5,
        normal_strength=1.5,
        octaves=3,
        frequencies=(1.0 / 32.0, 1.0 / 16.0, 1.0 / 8.0),
        amplitudes=(0.5, 0.3, 0.2),
        channels=ChannelSpec(("color", "roughness", "normal", "height")),
        grid_rows=2,
        grid_cols=2,
        tolerance=1e-6,
        overlap=2,
    )
    base.update(overrides)
    return MaterialSpec(**base)
