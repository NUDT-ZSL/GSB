"""确定性样本构造：清单 JSON 与配套贴图 PNG。

所有像素都由公式生成，不含随机数、时间戳或环境相关量；任何机器上
重复执行都会得到逐字节相同的样本，便于「同一份清单重复运行」的比对。

样本覆盖的条目（对应验收规则）：

* ``basic``    —— 3 个材质 × 2 个方向，全部合法（#1、#6、#9）
* ``bad_angle`` —— 一个材质含越界角度 + 一个合法角度（#2）
* ``missing``  —— 最后一个材质缺法线贴图（#3、#7）
* ``size``     —— 一个材质的两张贴图尺寸不一致，且与声明尺寸不符（#4）
* ``zero_normal`` —— 法线贴图含零向量像素（#5）
* ``all``      —— 以上全部条目合并成一份清单（#8）
* ``syntax_error`` —— 故意写坏的 JSON（#10），由测试直接构造，不生成 PNG
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

SIZE = 32

MATERIALS = {
    "marble": (200, 170, 150),
    "brick": (180, 80, 60),
    "metal": (120, 150, 190),
    "fabric": (90, 160, 110),
    "glass": (140, 200, 220),
    "soil": (120, 96, 64),
}


def _base_color_rgb(name: str, size: int = SIZE) -> np.ndarray:
    """这块基础色同时承担纹理坐标的「指纹」：

    * R 通道随 x 线性变化，
    * G 通道随 y 线性变化，
    * B 通道是棋盘格。
    两张不同方向的预览若共用同一份基础色，未受光区域就能按 R/G 定位到
    同一个源像素；棋盘格则让「位置是否移动」一眼可辨。
    """
    base_r, base_g, base_b = MATERIALS[name]
    ys, xs = np.mgrid[0:size, 0:size]
    checker = ((xs // 4 + ys // 4) % 2).astype(np.float64)

    r = base_r * (0.55 + 0.45 * xs / (size - 1))
    g = base_g * (0.55 + 0.45 * ys / (size - 1))
    b = base_b * (0.35 + 0.65 * checker)
    return np.stack([r, g, b], axis=2).round().clip(0, 255).astype(np.uint8)


def _normal_rgb(size: int = SIZE, zero_patch: bool = False) -> np.ndarray:
    """以左上方为光源的半球法线，可选在中间挖一块零向量。

    法线编码按 8 位标准：``pixel = round((v + 1) * 127.5)``。
    零向量写像素 (128,128,128) —— 反解后长度约 0.0136，正是真实
    美术素材里「忘记烘焙」的样子。
    """
    ys, xs = np.mgrid[0:size, 0:size]
    nx = 0.75 * (xs / (size - 1) * 2 - 1)
    ny = 0.75 * (ys / (size - 1) * 2 - 1)
    nz = np.sqrt(np.clip(1.0 - nx * nx - ny * ny, 0.0, 1.0))
    vectors = np.stack([nx, ny, nz], axis=2)
    pixels = np.rint((vectors + 1.0) * 127.5).clip(0, 255).astype(np.uint8)

    if zero_patch:
        lo, hi = size // 2 - 3, size // 2 + 3
        pixels[lo:hi, lo:hi] = 128
    return pixels


def _save_png(pixels: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels, mode="RGB").save(path, format="PNG", optimize=False)


def _material(name: str, angles: list, **overrides) -> dict:
    entry = {
        "name": name,
        "base_color": f"textures/{name}_basecolor.png",
        "normal_map": f"textures/{name}_normal.png",
        "light_angles": angles,
        "width": SIZE,
        "height": SIZE,
    }
    entry.update(overrides)
    return entry


def _write_textures(root: Path, names: list[str], zero_normal_for: set[str] | None = None) -> None:
    zero_normal_for = zero_normal_for or set()
    for name in names:
        _save_png(_base_color_rgb(name), root / "textures" / f"{name}_basecolor.png")
        _save_png(
            _normal_rgb(zero_patch=name in zero_normal_for),
            root / "textures" / f"{name}_normal.png",
        )


def _dump(root: Path, manifest: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    (root / "manifest.json").write_text(text, encoding="utf-8", newline="\n")


def _base_manifest(materials: list[dict], angle_min: float = 0, angle_max: float = 180) -> dict:
    return {
        "schema": "material-preview-manifest/v1",
        "root": ".",
        "light": {"angle_min": angle_min, "angle_max": angle_max},
        "neutral_color": [128, 128, 255],
        "materials": materials,
    }


# --- 各场景 ---------------------------------------------------------------


def build_basic(root: Path) -> None:
    """3 材质 × 2 方向 = 6 张预览，全部合法。"""
    _write_textures(root, ["marble", "brick", "metal"])
    _dump(
        root,
        _base_manifest(
            [
                _material("marble", [0, 90]),
                _material("brick", [45, 135]),
                _material("metal", [90, 180]),
            ]
        ),
    )


def build_bad_angle(root: Path) -> None:
    """marble 含 270°（越界）与 90°（合法），brick 全合法。"""
    _write_textures(root, ["marble", "brick"])
    _dump(
        root,
        _base_manifest(
            [
                _material("marble", [270, 90]),
                _material("brick", [45]),
            ]
        ),
    )


def build_missing(root: Path) -> None:
    """glass 缺法线贴图；marble / brick 必须照常渲染。"""
    _write_textures(root, ["marble", "brick"])
    _save_png(_base_color_rgb("glass"), root / "textures" / "glass_basecolor.png")
    _dump(
        root,
        _base_manifest(
            [
                _material("marble", [0, 90]),
                _material("brick", [45]),
                _material("glass", [30, 120]),
            ]
        ),
    )


def build_size(root: Path) -> None:
    """fabric 的基础色是 32x32，法线是 16x16，且与声明 32x32 不符。"""
    _save_png(_base_color_rgb("fabric", SIZE), root / "textures" / "fabric_basecolor.png")
    _save_png(_normal_rgb(size=16), root / "textures" / "fabric_normal.png")
    _dump(
        root,
        _base_manifest(
            [
                _material("fabric", [0, 90]),
            ]
        ),
    )


def build_zero_normal(root: Path) -> None:
    """glass 法线贴图中部 6x6 像素为零向量。"""
    _save_png(_base_color_rgb("glass"), root / "textures" / "glass_basecolor.png")
    _save_png(
        _normal_rgb(zero_patch=True), root / "textures" / "glass_normal.png"
    )
    _dump(
        root,
        _base_manifest(
            [
                _material("glass", [0, 180]),
            ]
        ),
    )


def build_all(root: Path) -> None:
    """把正常与各类异常条目合成一份清单，用于综合验证报告一致性。"""
    _write_textures(
        root,
        ["marble", "brick", "metal", "fabric", "glass", "soil"],
        zero_normal_for={"metal"},
    )
    # 覆盖 fabric：基础色 32x32，法线 16x16 —— 尺寸不一致。
    _save_png(_normal_rgb(size=16), root / "textures" / "fabric_normal.png")
    # 删掉 glass 的法线贴图 —— 缺失。
    (root / "textures" / "glass_normal.png").unlink()

    _dump(
        root,
        _base_manifest(
            [
                _material("marble", [0, 90]),
                _material("brick", [45, 135]),
                _material("metal", [180, 0]),
                _material("fabric", [30]),
                _material("glass", [60, 120]),
                _material("soil", [270, "north", 90]),
            ]
        ),
    )


def build_syntax_error(root: Path) -> None:
    """故意缺一个右括号；同时放一张贴图，验证程序不会碰它们。"""
    _write_textures(root, ["marble"])
    root.mkdir(parents=True, exist_ok=True)
    broken = (
        '{\n'
        '  "root": ".",\n'
        '  "light": {"angle_min": 0, "angle_max": 180},\n'
        '  "materials": [\n'
        '    {"name": "marble", "base_color": "textures/marble_basecolor.png",\n'
    )
    (root / "manifest.json").write_text(broken, encoding="utf-8", newline="\n")


BUILDERS = {
    "basic": build_basic,
    "bad_angle": build_bad_angle,
    "missing": build_missing,
    "size": build_size,
    "zero_normal": build_zero_normal,
    "all": build_all,
    "syntax_error": build_syntax_error,
}


def build(scenario: str, root: Path) -> Path:
    try:
        builder = BUILDERS[scenario]
    except KeyError:
        raise SystemExit(
            f"未知样例 {scenario!r}，可选：{', '.join(sorted(BUILDERS))}"
        ) from None
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"样例目录 {root} 非空，请先删除或换一个路径")
    builder(root)
    return root / "manifest.json"


if __name__ == "__main__":
    from .samples_cli import main

    raise SystemExit(main())
