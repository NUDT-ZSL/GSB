"""清单（manifest）JSON 的读取与校验。

清单是唯一的事实来源：材质、贴图路径、水平光照角度范围都在这里声明。
本模块只做「解析 + 结构校验 + 角度合法性判定」，不做任何渲染，
因此清单语法错误可以在写任何输出文件之前就被拦下来。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(Exception):
    """清单无法被接受时抛出，message 面向人阅读。"""


@dataclass(frozen=True)
class MaterialSpec:
    name: str
    base_color: str
    normal_map: str
    light_angles: tuple[Any, ...]
    width: int
    height: int


@dataclass(frozen=True)
class Manifest:
    materials: tuple[MaterialSpec, ...]
    angle_min: float
    angle_max: float
    neutral_color: tuple[int, int, int]
    manifest_path: Path
    root: Path


DEFAULT_NEUTRAL = (128, 128, 255)


def _require(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ManifestError(f"{where} 缺少必填字段 {key!r}")
    return obj[key]


def angle_identity(angle: Any) -> Any:
    """把角度映射成可比较的键（90 与 90.0 视为同一个方向）。

    用于生成文件名时的去重后缀：清单里同一材质列了两次同一角度时，
    两条条目都必须有自己的产物，不能让后写的覆盖先写的。
    """
    if isinstance(angle, bool):
        return ("bool", angle)
    if isinstance(angle, (int, float)):
        return ("number", float(angle))
    try:
        return ("value", json.dumps(angle, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError):
        return ("repr", repr(angle))


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"JSON 不允许常量 {value}")


def load_manifest(path: Path) -> Manifest:
    """读取并校验清单。任何问题都以 ManifestError 报出。"""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError(f"清单文件不存在：{path}") from None
    except OSError as exc:
        raise ManifestError(f"无法读取清单 {path}：{exc}") from None
    except UnicodeDecodeError as exc:
        raise ManifestError(f"清单 {path} 不是合法的 UTF-8 文本：{exc}") from None

    try:
        data = json.loads(raw_text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"清单 JSON 语法错误：{path} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}"
        ) from None
    except ValueError as exc:
        raise ManifestError(f"清单 JSON 语法错误：{path}：{exc}") from None

    if not isinstance(data, dict):
        raise ManifestError("清单顶层必须是 JSON 对象")

    root_raw = data.get("root", ".")
    if not isinstance(root_raw, str):
        raise ManifestError("清单字段 'root' 必须是字符串（贴图相对目录）")
    root = (path.parent / root_raw).resolve()

    light_raw = data.get("light", {})
    if not isinstance(light_raw, dict):
        raise ManifestError("清单字段 'light' 必须是 JSON 对象")
    try:
        angle_min = float(_require(light_raw, "angle_min", "'light'"))
        angle_max = float(_require(light_raw, "angle_max", "'light'"))
    except (TypeError, ValueError):
        raise ManifestError("'light.angle_min' 与 'light.angle_max' 必须是数值") from None
    if not (math.isfinite(angle_min) and math.isfinite(angle_max)):
        raise ManifestError("'light.angle_min' 与 'light.angle_max' 必须是有限数值")
    if angle_min > angle_max:
        raise ManifestError(
            f"'light.angle_min' ({angle_min}) 不能大于 'light.angle_max' ({angle_max})"
        )

    neutral_raw = data.get("neutral_color", list(DEFAULT_NEUTRAL))
    if (
        not isinstance(neutral_raw, list)
        or len(neutral_raw) != 3
        or not all(
            isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255
            for c in neutral_raw
        )
    ):
        raise ManifestError(
            "'neutral_color' 必须是 3 个 0..255 的整数组成的数组，形如 [128,128,255]"
        )
    neutral = (neutral_raw[0], neutral_raw[1], neutral_raw[2])

    materials_raw = _require(data, "materials", "清单顶层")
    if not isinstance(materials_raw, list) or not materials_raw:
        raise ManifestError("清单字段 'materials' 必须是非空数组")

    seen_names: set[str] = set()
    materials: list[MaterialSpec] = []
    for index, item in enumerate(materials_raw):
        where = f"materials[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{where} 必须是 JSON 对象")

        name = _require(item, "name", where)
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"{where}.name 必须是非空字符串")
        # 文件系统大小写不敏感（Windows/macOS 默认）时，Marble 与 marble
        # 会落到同一组预览文件上，因此按不区分大小写判重。
        folded = name.casefold()
        if folded in seen_names:
            raise ManifestError(
                f"材质名重复：{name!r}（材质名需唯一，否则预览文件名会互相覆盖）"
            )
        seen_names.add(folded)

        base_color = _require(item, "base_color", where)
        normal_map = _require(item, "normal_map", where)
        if not isinstance(base_color, str) or not base_color:
            raise ManifestError(f"{where}.base_color 必须是非空字符串（相对 root 的路径）")
        if not isinstance(normal_map, str) or not normal_map:
            raise ManifestError(f"{where}.normal_map 必须是非空字符串（相对 root 的路径）")

        width = _require(item, "width", where)
        height = _require(item, "height", where)
        for key, value in (("width", width), ("height", height)):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ManifestError(f"{where}.{key} 必须是正整数")

        angles_raw = _require(item, "light_angles", where)
        if not isinstance(angles_raw, list) or not angles_raw:
            raise ManifestError(f"{where}.light_angles 必须是非空数组")

        materials.append(
            MaterialSpec(
                name=name,
                base_color=base_color,
                normal_map=normal_map,
                light_angles=tuple(angles_raw),
                width=width,
                height=height,
            )
        )

    return Manifest(
        materials=tuple(materials),
        angle_min=angle_min,
        angle_max=angle_max,
        neutral_color=neutral,
        manifest_path=path.resolve(),
        root=root,
    )


def angle_problem(angle: Any, manifest: Manifest) -> str | None:
    """返回该角度的不合法原因；合法时返回 None。

    规则 #2：只接受清单约定范围内的数值。越界、非数值、布尔、NaN/Inf
    都不渲染，并在报告中区分「未完成」。
    """
    if isinstance(angle, bool) or not isinstance(angle, (int, float)):
        return f"方向值不是数值（{angle!r}）"
    if isinstance(angle, float) and not math.isfinite(angle):
        return f"方向值不是有限数（{angle!r}）"
    if angle < manifest.angle_min or angle > manifest.angle_max:
        return (
            f"方向 {angle} 超出清单约定范围 "
            f"[{manifest.angle_min:g}, {manifest.angle_max:g}]"
        )
    return None
