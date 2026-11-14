"""场景 JSON 的读取、字段校验与数据结构定义。

校验分两层：

1. 场景结构（材质/球体/画布/背景/光照列表本身）非法 -> 抛出 ``ConfigError``，
   提示到具体字段路径，调用方必须整体拒绝、不产生任何输出；
2. 单个光照方向非法（零向量、含非数值、结构不对）-> 记录为 ``skipped``，
   其余有效方向照常渲染。

约定上限（超限整体拒绝）见模块级常量。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError

# ---- 约定上限 ---------------------------------------------------------------
MAX_CANVAS_WIDTH = 1920
MAX_CANVAS_HEIGHT = 1080
MAX_LIGHT_DIRECTIONS = 16
MIN_CANVAS_SIDE = 1


@dataclass(frozen=True)
class Material:
    """球体材质（场景内所有球体共用同一份材质）。"""

    ambient: tuple[float, float, float]
    diffuse: tuple[float, float, float]
    specular_intensity: float | None = None
    specular_exponent: float | None = None

    @property
    def has_specular(self) -> bool:
        """仅当高光强度与高光指数同时提供时才叠加高光。"""
        return self.specular_intensity is not None and self.specular_exponent is not None


@dataclass(frozen=True)
class Sphere:
    """球体：球心（画布平面 x/y，世界深度 z）与半径，单位为像素。"""

    center: tuple[float, float, float]
    radius: float


@dataclass(frozen=True)
class LightDirection:
    """一个光照方向条目。

    ``valid`` 为 False 时 ``direction`` 为 None，``skip_reason`` 给出可读原因；
    编号沿用其在原始光照列表中的下标。
    """

    index: int
    direction: tuple[float, float, float] | None
    skip_reason: str | None

    @property
    def valid(self) -> bool:
        return self.direction is not None


@dataclass(frozen=True)
class Scene:
    """通过结构校验后的完整场景。"""

    width: int
    height: int
    background: tuple[int, int, int]
    material: Material
    spheres: tuple[Sphere, ...]
    lights: tuple[LightDirection, ...] = field(default=())


# ---- 基础类型/范围工具 ------------------------------------------------------

def _is_int(value: Any) -> bool:
    """严格整数判定：bool 不属于整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """有限数值判定：bool、NaN、Infinity 一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _require(condition: bool, path: str, detail: str) -> None:
    if not condition:
        raise ConfigError(f"字段 {path} 非法：{detail}")


def _as_rgb_float(value: Any, path: str) -> tuple[float, float, float]:
    _require(isinstance(value, list) and len(value) == 3, path,
             "颜色必须是包含 3 个数值的数组 [r, g, b]")
    channels: list[float] = []
    for i, component in enumerate(value):
        _require(_is_number(component), f"{path}[{i}]",
                 "颜色分量必须是有限数值")
        fv = float(component)
        _require(0.0 <= fv <= 1.0, f"{path}[{i}]",
                 "颜色分量取值范围必须在 [0, 1]")
        channels.append(fv)
    return channels[0], channels[1], channels[2]


def _as_rgb_int(value: Any, path: str) -> tuple[int, int, int]:
    _require(isinstance(value, list) and len(value) == 3, path,
             "背景色必须是包含 3 个整数的数组 [r, g, b]")
    channels: list[int] = []
    for i, component in enumerate(value):
        _require(_is_int(component), f"{path}[{i}]",
                 "背景色分量必须是整数")
        _require(0 <= component <= 255, f"{path}[{i}]",
                 "背景色分量取值范围必须在 [0, 255]")
        channels.append(int(component))
    return channels[0], channels[1], channels[2]


# ---- 各结构块校验 -----------------------------------------------------------

def _parse_material(raw: Any) -> Material:
    path = "material"
    _require(isinstance(raw, dict), path, "材质必须是对象")

    _require("ambient" in raw, f"{path}.ambient", "缺少环境光颜色 ambient")
    _require("diffuse" in raw, f"{path}.diffuse", "缺少漫反射颜色 diffuse")
    ambient = _as_rgb_float(raw["ambient"], f"{path}.ambient")
    diffuse = _as_rgb_float(raw["diffuse"], f"{path}.diffuse")

    has_intensity = "specular_intensity" in raw
    has_exponent = "specular_exponent" in raw
    specular_intensity: float | None = None
    specular_exponent: float | None = None

    if has_intensity:
        value = raw["specular_intensity"]
        _require(_is_number(value), f"{path}.specular_intensity",
                 "高光强度必须是有限数值")
        specular_intensity = float(value)
        _require(0.0 <= specular_intensity <= 1.0, f"{path}.specular_intensity",
                 "高光强度取值范围必须在 [0, 1]")
    if has_exponent:
        value = raw["specular_exponent"]
        _require(_is_number(value), f"{path}.specular_exponent",
                 "高光指数必须是有限数值")
        specular_exponent = float(value)
        _require(specular_exponent > 0.0, f"{path}.specular_exponent",
                 "高光指数必须大于 0")

    # 高光强度与高光指数必须成对出现，否则视为配置错误整体拒绝。
    _require(has_intensity == has_exponent, path,
             "specular_intensity 与 specular_exponent 必须同时提供或同时缺省")
    return Material(ambient, diffuse, specular_intensity, specular_exponent)


def _parse_spheres(raw: Any) -> tuple[Sphere, ...]:
    path = "spheres"
    _require(isinstance(raw, list) and len(raw) > 0, path,
             "球体列表必须是非空数组")
    spheres: list[Sphere] = []
    for idx, item in enumerate(raw):
        sp_path = f"{path}[{idx}]"
        _require(isinstance(item, dict), sp_path, "球体必须是对象")

        _require("center" in item, f"{sp_path}.center", "缺少球心 center")
        center_raw = item["center"]
        _require(isinstance(center_raw, list) and len(center_raw) == 3,
                 f"{sp_path}.center", "球心必须是包含 3 个数值的数组 [x, y, z]")
        center: list[float] = []
        for axis, component in zip(("x", "y", "z"), center_raw):
            _require(_is_number(component), f"{sp_path}.center.{axis}",
                     "球心坐标必须是有限数值")
            center.append(float(component))

        _require("radius" in item, f"{sp_path}.radius", "缺少半径 radius")
        radius_raw = item["radius"]
        _require(_is_number(radius_raw), f"{sp_path}.radius",
                 "半径必须是有限数值")
        radius = float(radius_raw)
        _require(radius > 0.0, f"{sp_path}.radius", "半径必须大于 0")
        spheres.append(Sphere((center[0], center[1], center[2]), radius))
    return tuple(spheres)


def _parse_canvas(raw: Any) -> tuple[int, int]:
    path = "canvas"
    _require(isinstance(raw, dict), path, "画布必须是对象")
    _require("width" in raw, f"{path}.width", "缺少画布宽度 width")
    _require("height" in raw, f"{path}.height", "缺少画布高度 height")

    width_raw, height_raw = raw["width"], raw["height"]
    _require(_is_int(width_raw), f"{path}.width", "画布宽度必须是整数")
    _require(_is_int(height_raw), f"{path}.height", "画布高度必须是整数")
    width, height = int(width_raw), int(height_raw)

    _require(width >= MIN_CANVAS_SIDE and height >= MIN_CANVAS_SIDE,
             path, f"画布宽高必须 >= {MIN_CANVAS_SIDE}")
    _require(width <= MAX_CANVAS_WIDTH, f"{path}.width",
             f"画布宽度 {width} 超过上限 {MAX_CANVAS_WIDTH}")
    _require(height <= MAX_CANVAS_HEIGHT, f"{path}.height",
             f"画布高度 {height} 超过上限 {MAX_CANVAS_HEIGHT}")
    return width, height


def _parse_lights(raw: Any) -> tuple[LightDirection, ...]:
    path = "lights"
    _require(isinstance(raw, list), path, "光照方向列表必须是数组")
    _require(len(raw) <= MAX_LIGHT_DIRECTIONS, path,
             f"光照方向数量 {len(raw)} 超过上限 {MAX_LIGHT_DIRECTIONS}")

    lights: list[LightDirection] = []
    for idx, item in enumerate(raw):
        light_path = f"{path}[{idx}]"

        if not isinstance(item, dict) or "direction" not in item:
            lights.append(LightDirection(idx, None,
                                         f"{light_path} 缺少 direction 字段或条目不是对象"))
            continue

        direction_raw = item["direction"]
        if not isinstance(direction_raw, list) or len(direction_raw) != 3:
            lights.append(LightDirection(
                idx, None,
                f"{light_path}.direction 必须是包含 3 个数值的数组 [x, y, z]"))
            continue

        bad_axis: str | None = None
        components: list[float] = []
        for axis, component in zip(("x", "y", "z"), direction_raw):
            if not _is_number(component):
                bad_axis = axis
                break
            components.append(float(component))
        if bad_axis is not None:
            lights.append(LightDirection(
                idx, None,
                f"{light_path}.direction.{bad_axis} 含非数值（必须是有限数值）"))
            continue

        length = math.sqrt(sum(c * c for c in components))
        if length == 0.0:
            lights.append(LightDirection(
                idx, None, f"{light_path}.direction 是零向量，无法确定光照方向"))
            continue

        normalized = (components[0] / length,
                      components[1] / length,
                      components[2] / length)
        lights.append(LightDirection(idx, normalized, None))
    return tuple(lights)


# ---- 入口 -------------------------------------------------------------------

def load_scene(path: str) -> Scene:
    """从 JSON 文件加载并校验场景。结构非法时抛出 :class:`ConfigError`。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"场景文件不存在：{path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"场景文件不是合法 JSON：{exc}") from None
    except OSError as exc:
        raise ConfigError(f"场景文件读取失败：{exc}") from None

    if not isinstance(raw, dict):
        raise ConfigError("场景根节点必须是 JSON 对象")

    for required in ("material", "spheres", "lights", "canvas", "background"):
        _require(required in raw, required, f"缺少顶层字段 {required}")

    material = _parse_material(raw["material"])
    spheres = _parse_spheres(raw["spheres"])
    width, height = _parse_canvas(raw["canvas"])
    background = _as_rgb_int(raw["background"], "background")
    lights = _parse_lights(raw["lights"])

    return Scene(width=width, height=height, background=background,
                 material=material, spheres=spheres, lights=lights)
