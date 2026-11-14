"""帧序列索引、帧号定位与帧文件读取。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

from .config import Config
from .errors import FrameReadError, SetupError

# 允许读取被截断但仍有可用内容的 PNG，避免单帧异常中断整批任务。
ImageFile.LOAD_TRUNCATED_IMAGES = True

_DIGIT_GROUPS = re.compile(r"\d+")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class FrameMatch:
    index: int
    path: Path


def _index_from_name(stem: str) -> int | None:
    """取文件名中最长的数字段作为帧号（同长时取靠后者）。

    这样 ``frame_0001``、``shot_v2_frame_0010``、``0005_result`` 都能得到符合
    直觉的帧号；完全没有数字的文件返回 None。
    """

    groups = _DIGIT_GROUPS.findall(stem)
    if not groups:
        return None
    longest = max(len(group) for group in groups)
    for group in reversed(groups):
        if len(group) == longest:
            return int(group)
    return None


def scan_frames(frame_dir: Path) -> dict[int, list[Path]]:
    """返回“帧号 -> 文件列表”。"""

    if not frame_dir.is_dir():
        raise SetupError(f"帧序列目录不存在或不是目录：{frame_dir}")

    files = sorted(
        (p for p in frame_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
        key=lambda p: p.name,
    )
    if not files:
        raise SetupError(f"帧序列目录中没有可识别的图片：{frame_dir}")

    index: dict[int, list[Path]] = {}
    unnamed: list[Path] = []
    for path in files:
        number = _index_from_name(path.stem)
        if number is None:
            unnamed.append(path)
        else:
            index.setdefault(number, []).append(path)

    # 文件名不含数字时，接在已有最大帧号之后按文件名字典序编号。
    next_number = max(index, default=-1) + 1
    for path in unnamed:
        index.setdefault(next_number, []).append(path)
        next_number += 1
    return index


def target_frame_number(time_seconds: float, config: Config) -> int:
    """时间点换算出的目标帧号：frame_start + time * fps，四舍五入（>.5 进位）。"""

    raw = config.frame_start + time_seconds * config.fps
    return int(math.floor(raw + 0.5))


def locate_frame(
    time_seconds: float,
    config: Config,
    index: dict[int, list[Path]],
) -> tuple[FrameMatch | None, list[str]]:
    """按时间点定位帧文件。

    返回 (命中的帧, 备注列表)。定位失败时命中为 None，备注中给出原因。
    优先精确命中的帧号；否则只在“与目标帧号的差值不超过 tolerance_frames”的
    帧号中选择最接近者。容差之外的帧不会被当作命中的替代帧。
    """

    target = target_frame_number(time_seconds, config)
    if target < 0:
        return None, [
            f"时间点 {time_seconds:g}s 换算为帧号 {target}，小于 0，超出帧序列范围"
        ]

    tolerance = config.tolerance_frames
    chosen: int | None = None
    if target in index:
        chosen = target
    elif tolerance > 0:
        low = int(math.ceil(target - tolerance - 1e-9))
        high = int(math.floor(target + tolerance + 1e-9))
        candidates = [
            n for n in range(low, high + 1) if n in index and abs(n - target) <= tolerance + 1e-9
        ]
        if candidates:
            chosen = min(candidates, key=lambda n: (abs(n - target), n))

    if chosen is None:
        detail = f"时间点 {time_seconds:g}s 换算为帧号 {target}"
        if index and target > max(index):
            detail += f"，超出帧序列范围（最大帧号 {max(index)}）"
        elif index and target < min(index):
            detail += f"，早于帧序列起始（最小帧号 {min(index)}）"
        elif tolerance > 0:
            detail += (
                f"，容差 ±{tolerance:g} 帧内无对应帧文件"
                f"（检查帧号 {max(0, int(math.ceil(target - tolerance - 1e-9)))}"
                f"~{int(math.floor(target + tolerance + 1e-9))}）"
            )
        else:
            detail += "，目录中没有该帧文件"
        if index:
            nearest = min(index, key=lambda n: (abs(n - target), n))
            if nearest != target:
                detail += f"；序列中最近的帧号为 {nearest}"
        return None, [detail]

    paths = index[chosen]
    notes: list[str] = []
    if chosen != target:
        notes.append(f"按容差 ±{tolerance:g} 帧定位到帧号 {chosen}（目标帧号 {target}）")
    if len(paths) > 1:
        names = "、".join(p.name for p in paths)
        notes.append(f"帧号 {chosen} 对应多个文件（{names}），取 {paths[0].name}")
    return FrameMatch(index=chosen, path=paths[0]), notes


def _normalize_image(
    path: Path, config: Config
) -> tuple[Image.Image, tuple[int, int], list[str]]:
    """打开帧并统一到基准尺寸，保留原始色彩模式（RGB 输出）。"""

    try:
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            original = image.size
            rgb = image.convert("RGB")
    except Exception as exc:  # Pillow 对损坏文件可能抛出多种异常
        raise FrameReadError(f"无法解码帧文件 {path.name}：{exc}") from exc

    base = (config.base_width, config.base_height)
    notes: list[str] = []
    if original != base:
        if config.resize_mode == "stretch":
            notes.append(
                f"原尺寸 {original[0]}x{original[1]} 与基准 {base[0]}x{base[1]} 不一致，"
                "拉伸到基准尺寸后参与评分"
            )
            rgb = rgb.resize(base, Image.Resampling.LANCZOS)
        else:
            # 等比缩放后用画面平均色留边，避免黑边拉高边缘梯度、干扰排序。
            content = ImageOps.contain(rgb, base, method=Image.Resampling.LANCZOS)
            fill = tuple(int(round(value)) for value in np.asarray(content).mean(axis=(0, 1)))
            notes.append(
                f"原尺寸 {original[0]}x{original[1]} 与基准 {base[0]}x{base[1]} 不一致，"
                f"按比例缩放后用画面平均色 {fill} 留边到基准尺寸后参与评分"
            )
            rgb = ImageOps.pad(content, base, color=fill, method=Image.Resampling.NEAREST)
    return rgb, original, notes


def load_frame_gray(path: Path, config: Config) -> tuple[np.ndarray, list[str], bool]:
    """读取帧并统一到基准尺寸。

    返回 (灰度 uint8 数组, 处理备注, 是否做过尺寸统一)。尺寸不一致时按配置的
    resize_mode 统一，不因单帧尺寸异常中断；无法解码时抛出 :class:`FrameReadError`。
    """

    rgb, original, notes = _normalize_image(path, config)
    resized = original != (config.base_width, config.base_height)
    return np.asarray(rgb.convert("L"), dtype=np.uint8), notes, resized


def load_frame_image(path: Path, config: Config) -> Image.Image:
    """读取用于导出的帧，统一到基准尺寸并返回 RGB 图像。"""

    return _normalize_image(path, config)[0]
