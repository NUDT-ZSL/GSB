"""材质清单 / 生成配置的数据模型与解析校验。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode

REQUIRED_CHANNELS = ("color", "roughness", "normal")


def _is_pos_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_finite_pos(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value > 0


@dataclass(frozen=True)
class ChannelSpec:
    """通道声明；height 为可选调试通道，三个业务通道为强制项。"""

    names: tuple[str, ...]

    @property
    def has_color(self) -> bool:
        return "color" in self.names

    @property
    def has_roughness(self) -> bool:
        return "roughness" in self.names

    @property
    def has_normal(self) -> bool:
        return "normal" in self.names


@dataclass(frozen=True)
class MaterialSpec:
    material_id: str
    seed: int
    width: int
    height: int
    base_color: tuple[float, float, float]
    roughness: float
    normal_strength: float
    octaves: int
    frequencies: tuple[float, ...]
    amplitudes: tuple[float, ...]
    channels: ChannelSpec
    grid_rows: int
    grid_cols: int
    tolerance: float
    overlap: int

    def canonical_dict(self) -> dict:
        """规范化字典：用于版本标识，键序固定，仅含影响结果的参数。"""
        return {
            "material_id": self.material_id,
            "seed": self.seed,
            "width": self.width,
            "height": self.height,
            "base_color": list(self.base_color),
            "roughness": self.roughness,
            "normal_strength": self.normal_strength,
            "octaves": self.octaves,
            "frequencies": list(self.frequencies),
            "amplitudes": list(self.amplitudes),
            "channels": list(self.channels.names),
            "grid_rows": self.grid_rows,
            "grid_cols": self.grid_cols,
            "overlap": self.overlap,
        }


@dataclass(frozen=True)
class GenConfig:
    grid_rows: int = 2
    grid_cols: int = 2
    concurrency: int = 2
    memory_budget_bytes: int = 64 * 1024 * 1024
    overlap: int = 2
    tolerance: float = 1e-6
    output_dir: str = "build/output"
    state_dir: str = "build/state"
    archive_dir: str = "build/archive"
    dtype: str = "float32"

    def canonical_dict(self) -> dict:
        # 输出路径、并发、预算、容差不参与噪声结果，故不进入版本标识。
        return {
            "grid_rows": self.grid_rows,
            "grid_cols": self.grid_cols,
            "overlap": self.overlap,
            "dtype": self.dtype,
        }


def load_config(path: str | Path) -> GenConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("生成配置根节点必须是 JSON 对象")

    def req_pos_int(key: str, default: int) -> int:
        value = raw.get(key, default)
        if not _is_pos_int(value):
            raise ValueError(f"配置项 {key} 必须为正整数，实际为 {value!r}")
        return int(value)

    grid_rows = req_pos_int("grid_rows", 2)
    grid_cols = req_pos_int("grid_cols", 2)
    concurrency = req_pos_int("concurrency", 2)
    overlap = req_pos_int("overlap", 2)
    budget = req_pos_int("memory_budget_bytes", 64 * 1024 * 1024)

    tolerance = raw.get("tolerance", 1e-6)
    if not _is_finite_pos(tolerance):
        raise ValueError(f"配置项 tolerance 必须为有限正数，实际为 {tolerance!r}")

    dtype = raw.get("dtype", "float32")
    if dtype not in ("float32", "float64"):
        raise ValueError(f"配置项 dtype 仅支持 float32/float64，实际为 {dtype!r}")

    return GenConfig(
        grid_rows=grid_rows,
        grid_cols=grid_cols,
        concurrency=concurrency,
        memory_budget_bytes=budget,
        overlap=overlap,
        tolerance=float(tolerance),
        output_dir=str(raw.get("output_dir", "build/output")),
        state_dir=str(raw.get("state_dir", "build/state")),
        archive_dir=str(raw.get("archive_dir", "build/archive")),
        dtype=dtype,
    )


def validate_material(raw: Any, config: GenConfig) -> tuple[MaterialSpec | None, BusinessError | None]:
    """校验单条材质。合法返回 (spec, None)，非法返回 (None, BusinessError)。"""

    if not isinstance(raw, dict):
        return None, BusinessError("<unknown>", ErrorCode.E_CONFIG_INVALID, "材质条目必须是 JSON 对象")

    material_id = raw.get("material_id")
    if not isinstance(material_id, str) or not material_id:
        return None, BusinessError(str(material_id or "<unknown>"), ErrorCode.E_CONFIG_INVALID, "缺少合法 material_id")

    def fail(code: str, message: str) -> tuple[None, BusinessError]:
        return None, BusinessError(material_id, code, message)

    width = raw.get("width")
    height = raw.get("height")
    if not (_is_pos_int(width) and _is_pos_int(height)):
        return fail(ErrorCode.E_DIMENSION_INVALID, f"width/height 必须为正整数，实际为 {width!r}/{height!r}")

    base_color = raw.get("base_color")
    if (
        not isinstance(base_color, list)
        or len(base_color) != 3
        or not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(float(c)) for c in base_color)
        or not all(0.0 <= float(c) <= 1.0 for c in base_color)
    ):
        return fail(ErrorCode.E_BASE_COLOR_RANGE, f"base_color 必须是三个 [0,1] 内有限数，实际为 {base_color!r}")

    roughness = raw.get("roughness")
    if not _is_finite_pos(roughness):
        return fail(ErrorCode.E_ROUGHNESS_INVALID, f"roughness 必须为有限正数，实际为 {roughness!r}")

    normal_strength = raw.get("normal_strength")
    if not _is_finite_pos(normal_strength):
        return fail(ErrorCode.E_NORMAL_STRENGTH_INVALID, f"normal_strength 必须为有限正数，实际为 {normal_strength!r}")

    octaves = raw.get("octaves")
    if not _is_pos_int(octaves):
        return fail(ErrorCode.E_OCTAVES_INVALID, f"octaves 必须为正整数，实际为 {octaves!r}")

    frequencies = raw.get("frequencies")
    if (
        not isinstance(frequencies, list)
        or len(frequencies) != octaves
        or not all(isinstance(f, (int, float)) and not isinstance(f, bool) and math.isfinite(float(f)) for f in frequencies)
        or any(float(frequencies[i]) >= float(frequencies[i + 1]) for i in range(len(frequencies) - 1))
    ):
        return fail(ErrorCode.E_FREQUENCY_ORDER, f"frequencies 长度需等于 octaves 且严格递增，实际为 {frequencies!r}")

    amplitudes = raw.get("amplitudes")
    if (
        not isinstance(amplitudes, list)
        or len(amplitudes) != octaves
        or not all(_is_finite_pos(a) for a in amplitudes)
    ):
        return fail(ErrorCode.E_AMPLITUDE_INVALID, f"amplitudes 长度需等于 octaves 且每项为正，实际为 {amplitudes!r}")

    channels_raw = raw.get("channels")
    if not isinstance(channels_raw, list) or not all(isinstance(c, str) for c in channels_raw):
        return fail(ErrorCode.E_CHANNEL_MISSING, f"channels 必须是字符串数组，实际为 {channels_raw!r}")
    channel_names = tuple(dict.fromkeys(channels_raw))
    missing = [name for name in REQUIRED_CHANNELS if name not in channel_names]
    if missing:
        return fail(ErrorCode.E_CHANNEL_MISSING, f"缺少强制通道声明: {', '.join(missing)}")

    seed = raw.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        return fail(ErrorCode.E_CONFIG_INVALID, f"seed 必须为整数，实际为 {seed!r}")

    grid_rows = raw.get("grid_rows", config.grid_rows)
    grid_cols = raw.get("grid_cols", config.grid_cols)
    if not (_is_pos_int(grid_rows) and _is_pos_int(grid_cols)):
        return fail(ErrorCode.E_GRID_DIVISIBILITY, f"分块行列必须为正整数，实际为 {grid_rows!r}/{grid_cols!r}")
    if width % grid_cols != 0 or height % grid_rows != 0:
        return fail(
            ErrorCode.E_GRID_DIVISIBILITY,
            f"分块网格 {grid_rows}x{grid_cols} 不能整除纹理尺寸 {width}x{height}",
        )

    spec = MaterialSpec(
        material_id=material_id,
        seed=int(seed),
        width=int(width),
        height=int(height),
        base_color=tuple(float(c) for c in base_color),  # type: ignore[arg-type]
        roughness=float(roughness),
        normal_strength=float(normal_strength),
        octaves=int(octaves),
        frequencies=tuple(float(f) for f in frequencies),
        amplitudes=tuple(float(a) for a in amplitudes),
        channels=ChannelSpec(channel_names),
        grid_rows=int(grid_rows),
        grid_cols=int(grid_cols),
        tolerance=config.tolerance,
        overlap=config.overlap,
    )
    return spec, None


def load_materials(path: str | Path, config: GenConfig) -> tuple[list[MaterialSpec], list[BusinessError]]:
    """读取材质清单；非法条目按 material_id 记录错误并继续处理其余条目。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("materials"), list):
        raise ValueError("材质清单根节点必须是含 materials 数组的 JSON 对象")

    specs: list[MaterialSpec] = []
    errors: list[BusinessError] = []
    for raw in payload["materials"]:
        spec, error = validate_material(raw, config)
        if spec is not None:
            specs.append(spec)
        if error is not None:
            errors.append(error)
    return specs, errors
