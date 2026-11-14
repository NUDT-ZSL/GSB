"""贴图读取与校验。

一个材质只有「基础色贴图」和「法线贴图」都可用、并且尺寸与清单声明一致时，
才允许进入渲染。所有失败都以 MaterialTextureError 携带面向人的原因返回，
由上层把该材质的全部方向标为未完成，而不是半途而废地渲染一部分。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .manifest import MaterialSpec

# 法线贴图里「零向量」的判定阈值。
#
# 8 位法线贴图把单位向量编码为 (v + 1) * 127.5，真正的零向量 (0,0,0)
# 落到像素 (128,128,128)，解码后是 (0.0078, 0.0078, 0.0078)，长度约 0.0136。
# 量化和取整会让「零」附近还有几个 LSB 的抖动，因此用 2.5 个色阶
# 作为容差：长度 ≤ 2.5/255 的向量一律按零向量处理，直接输出中性色。
ZERO_NORMAL_EPSILON = 2.5 / 255.0


class MaterialTextureError(Exception):
    """该材质无法渲染（缺文件、读不出、尺寸不符）。"""


@dataclass(frozen=True)
class BaseColorMap:
    pixels: np.ndarray  # (H, W, 3) float64，0..1
    path: Path
    size: tuple[int, int]


@dataclass(frozen=True)
class NormalMap:
    vectors: np.ndarray  # (H, W, 3) float64，单位向量（零向量保持为 0）
    zero_mask: np.ndarray  # (H, W) bool，True 表示该处是零向量
    path: Path
    size: tuple[int, int]


def _open_rgb(path: Path) -> np.ndarray:
    """把贴图解码成 (H, W, 3) uint8；任何解码问题都转成可读原因。"""
    if path.is_dir():
        raise MaterialTextureError(f"贴图路径是目录而不是文件：{path}")
    try:
        with Image.open(path) as image:
            image.load()
            rgb = image.convert("RGB")
    except FileNotFoundError:
        raise MaterialTextureError(f"贴图文件不存在：{path}") from None
    except Exception as exc:  # Pillow 对损坏/不支持的文件可能抛各种异常
        raise MaterialTextureError(f"贴图文件无法解码：{path}（{exc}）") from None
    return np.asarray(rgb, dtype=np.uint8)


def _resolve(root: Path, relative: str) -> Path:
    return (root / relative).resolve()


def load_base_color(spec: MaterialSpec, root: Path) -> BaseColorMap:
    path = _resolve(root, spec.base_color)
    if not path.is_file():
        raise MaterialTextureError(f"基础色贴图缺失：{spec.base_color}")
    rgb = _open_rgb(path)
    height, width = rgb.shape[:2]
    if (width, height) != (spec.width, spec.height):
        raise MaterialTextureError(
            f"基础色贴图尺寸 {width}x{height} 与清单声明 {spec.width}x{spec.height} 不符"
        )
    return BaseColorMap(
        pixels=rgb.astype(np.float64) / 255.0,
        path=path,
        size=(width, height),
    )


def load_normal_map(spec: MaterialSpec, root: Path) -> NormalMap:
    path = _resolve(root, spec.normal_map)
    if not path.is_file():
        raise MaterialTextureError(f"法线贴图缺失：{spec.normal_map}")
    rgb = _open_rgb(path)
    height, width = rgb.shape[:2]
    if (width, height) != (spec.width, spec.height):
        raise MaterialTextureError(
            f"法线贴图尺寸 {width}x{height} 与清单声明 {spec.width}x{spec.height} 不符"
        )

    vectors = rgb.astype(np.float64) / 127.5 - 1.0
    length = np.sqrt((vectors * vectors).sum(axis=2))
    zero_mask = length <= ZERO_NORMAL_EPSILON
    # 只有非零向量才归一化；零向量的长度留在 0，由渲染层用中性色替换。
    safe_length = np.where(zero_mask, 1.0, length)
    vectors = vectors / safe_length[..., None]

    return NormalMap(
        vectors=vectors,
        zero_mask=zero_mask,
        path=path,
        size=(width, height),
    )


def load_material_textures(
    spec: MaterialSpec, root: Path
) -> tuple[BaseColorMap, NormalMap]:
    """按顺序校验两张贴图，返回可直接渲染的数据。

    先分别校验与清单声明尺寸的一致性，再校验两张贴图彼此一致，
    这样报告能给出最贴近根因的原因。
    """
    base = load_base_color(spec, root)
    normal = load_normal_map(spec, root)
    if base.size != normal.size:
        raise MaterialTextureError(
            f"基础色贴图尺寸 {base.size[0]}x{base.size[1]} 与法线贴图尺寸 "
            f"{normal.size[0]}x{normal.size[1]} 不一致"
        )
    return base, normal
