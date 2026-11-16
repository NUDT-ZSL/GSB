"""Dual-version exchange file parsers and writers.

v1: binary container, 1-bit run-length encoded mask, single integer style
    intensity档位.
v2: text JSON, base64 per-pixel 0/255 mask bytes, per-channel float weights
    and a named palette.

Both versions parse into the same UnifiedProject structure.
"""
from __future__ import annotations

import base64
import json
import os
import struct
from pathlib import Path
from typing import Tuple

import numpy as np

from .errors import FormatInvalid, VersionUnsupported
from .models import (MASK_DAMAGED, MASK_INTACT, PaletteEntry, StyleParams,
                     UnifiedProject)

MAGIC_V1 = b"IED1"
MAGIC_V2 = b"IED2"
SUPPORTED_VERSIONS = (1, 2)


# --------------------------------------------------------------------------- #
# v1 binary
# --------------------------------------------------------------------------- #
def _decode_rle_v1(payload: bytes, pixel_count: int,
                   source_path: str) -> np.ndarray:
    bits = np.empty(pixel_count, dtype=np.uint8)
    cursor = 0
    run_index = 0
    pos = 0
    while pos < len(payload):
        if pos + 1 >= len(payload):
            raise FormatInvalid(
                "非法行程：行程头被截断 (truncated RLE header)",
                context={"run_index": run_index, "byte_offset": pos,
                         "source_path": source_path})
        value = payload[pos]
        run_len = payload[pos + 1]
        if value not in (0, 1):
            raise FormatInvalid(
                f"非法行程：行程值必须为 0/1，实际为 {value}",
                context={"run_index": run_index, "value": value,
                         "source_path": source_path})
        if run_len == 0:
            raise FormatInvalid(
                "非法行程：行程长度不得为 0",
                context={"run_index": run_index, "source_path": source_path})
        if cursor + run_len > pixel_count:
            raise FormatInvalid(
                "非法行程：累计行程长度超过图像像素总数",
                context={"run_index": run_index, "cursor": cursor,
                         "run_len": run_len, "pixel_count": pixel_count,
                         "source_path": source_path})
        bits[cursor:cursor + run_len] = value
        cursor += run_len
        run_index += 1
        pos += 2
    if cursor != pixel_count:
        raise FormatInvalid(
            f"非法行程：总行程长度 {cursor} 与像素总数 {pixel_count} 不符",
            context={"cursor": cursor, "pixel_count": pixel_count,
                     "source_path": source_path})
    return bits


def _encode_rle_v1(bitmap: np.ndarray) -> bytes:
    flat = bitmap.reshape(-1).astype(np.uint8)
    out = bytearray()
    if flat.size == 0:
        return bytes(out)
    run_value = int(flat[0])
    run_len = 1
    for value in flat[1:]:
        value = int(value)
        if value == run_value and run_len < 255:
            run_len += 1
        else:
            out.append(run_value)
            out.append(run_len)
            run_value = value
            run_len = 1
    out.append(run_value)
    out.append(run_len)
    return bytes(out)


def parse_v1(path: str | os.PathLike) -> UnifiedProject:
    path = str(path)
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        raise FormatInvalid(f"v1 工程无法读取: {exc}",
                            context={"source_path": path})
    offset = 0

    def take(fmt: str, nbytes: int, what: str):
        nonlocal offset
        if offset + nbytes > len(blob):
            raise FormatInvalid(
                f"v1 工程截断：缺少字段 {what}",
                context={"field": what, "offset": offset,
                         "source_path": path})
        values = struct.unpack_from(fmt, blob, offset)
        offset += nbytes
        return values

    magic, = take("<4s", 4, "magic")
    if magic != MAGIC_V1:
        if magic[:3] == b"IED":
            raise VersionUnsupported(
                f"未知版本号: magic={magic!r}",
                context={"magic": magic.decode("ascii", "replace"),
                         "source_path": path})
        raise FormatInvalid(
            f"v1 magic 不匹配: {magic!r}",
            context={"magic": repr(magic), "source_path": path})
    version, = take("<H", 2, "version")
    if version != 1:
        raise VersionUnsupported(
            f"未知版本号: {version}",
            context={"version": version, "source_path": path})
    width, = take("<I", 4, "width")
    height, = take("<I", 4, "height")
    intensity, = take("<i", 4, "intensity")
    image_len, = take("<H", 2, "image_path_len")
    image_path, = take(f"<{image_len}s", image_len, "image_path")
    ref_len, = take("<H", 2, "style_ref_len")
    ref_path, = take(f"<{ref_len}s", ref_len, "style_ref_path")
    run_count, = take("<I", 4, "run_count")

    if width == 0 or height == 0 or width > 65535 or height > 65535:
        raise FormatInvalid(
            f"v1 非法尺寸: {width}x{height}",
            context={"width": width, "height": height, "source_path": path})
    payload = blob[offset:]
    if len(payload) != run_count * 2:
        raise FormatInvalid(
            f"非法行程：RLE 字节数 {len(payload)} 与声明行程数 {run_count} 不符",
            context={"run_count": run_count, "payload_bytes": len(payload),
                     "source_path": path})

    bits = _decode_rle_v1(payload, width * height, path)
    mask = np.where(bits.reshape(height, width) == 1,
                    MASK_DAMAGED, MASK_INTACT).astype(np.uint8)
    style = StyleParams(intensity=int(intensity),
                        channel_weights={}, palette=[])
    return UnifiedProject(
        project_id=Path(path).stem,
        source_version=1,
        image_path=image_path.decode("utf-8"),
        style_ref_path=ref_path.decode("utf-8"),
        mask=mask,
        style=style,
        meta={"encoding": "binary_rle_1bit", "run_count": int(run_count)},
    )


def write_v1(project: UnifiedProject, path: str | os.PathLike) -> None:
    bitmap = project.mask_bitmap()
    rle = _encode_rle_v1(bitmap)
    image_bytes = project.image_path.encode("utf-8")
    ref_bytes = project.style_ref_path.encode("utf-8")
    if len(image_bytes) > 65535 or len(ref_bytes) > 65535:
        raise FormatInvalid("v1 路径长度超出 uint16 范围")
    header = struct.pack(
        "<4sHIIiH", MAGIC_V1, 1, project.mask.shape[1],
        project.mask.shape[0], int(project.style.intensity),
        len(image_bytes))
    body = struct.pack(f"<{len(image_bytes)}sH", image_bytes, len(ref_bytes))
    body += struct.pack(f"<{len(ref_bytes)}s", ref_bytes)
    run_count = len(rle) // 2
    body += struct.pack("<I", run_count)
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(header + body + rle)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# v2 JSON
# --------------------------------------------------------------------------- #
def parse_v2(path: str | os.PathLike) -> UnifiedProject:
    path = str(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise FormatInvalid(f"v2 工程无法读取: 文件不存在 {path}",
                            context={"source_path": path})
    except json.JSONDecodeError as exc:
        raise FormatInvalid(f"v2 工程 JSON 解析失败: {exc}",
                            context={"line": exc.lineno, "source_path": path})

    magic = data.get("magic")
    if magic != "IED2":
        if isinstance(magic, str) and magic.startswith("IED"):
            raise VersionUnsupported(
                f"未知版本号: magic={magic}",
                context={"magic": magic, "source_path": path})
        raise FormatInvalid("v2 magic 缺失或不匹配",
                            context={"magic": magic, "source_path": path})
    version = data.get("version")
    if version != 2:
        raise VersionUnsupported(
            f"未知版本号: {version}",
            context={"version": version, "source_path": path})

    for key in ("project_id", "width", "height", "image_path",
                "style_ref_path", "mask_base64", "style"):
        if key not in data:
            raise FormatInvalid(f"v2 工程缺少必填字段 {key}",
                                context={"missing": key, "source_path": path})
    width, height = int(data["width"]), int(data["height"])
    pixel_count = width * height

    encoded = data["mask_base64"]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise FormatInvalid(
            f"base64 掩码解码失败: {exc}",
            context={"param_key": "mask_base64", "source_path": path})
    if len(raw) != pixel_count:
        raise FormatInvalid(
            f"base64 长度不符: 解码字节 {len(raw)} != 像素总数 {pixel_count}",
            context={"decoded_bytes": len(raw),
                     "pixel_count": pixel_count, "source_path": path})
    bad = [b for b in raw if b not in (0, 255)]
    if bad:
        raise FormatInvalid(
            f"掩码像素值必须为 0/255，发现非法值 {bad[0]}",
            context={"bad_value": bad[0], "source_path": path})
    mask = np.frombuffer(raw, dtype=np.uint8).reshape(height, width).copy()

    style_data = data["style"]
    weights = style_data.get("channel_weights")
    if not isinstance(weights, dict):
        raise FormatInvalid("v2 缺少分通道权重对象 channel_weights",
                            context={"param_key": "style.channel_weights",
                                     "source_path": path})
    missing = [c for c in ("R", "G", "B") if c not in weights]
    if missing:
        raise FormatInvalid(
            f"缺失通道权重: {missing}",
            context={"param_key": "style.channel_weights",
                     "missing": missing, "source_path": path})
    channel_weights = {c: float(weights[c]) for c in ("R", "G", "B")}
    palette = [PaletteEntry.from_dict(p) for p in style_data.get("palette", [])]
    intensity = int(style_data.get("intensity", 0))
    style = StyleParams(intensity=intensity,
                        channel_weights=channel_weights, palette=palette)

    return UnifiedProject(
        project_id=str(data["project_id"]),
        source_version=2,
        image_path=str(data["image_path"]),
        style_ref_path=str(data["style_ref_path"]),
        mask=mask,
        style=style,
        meta={"encoding": "json_base64_8bit",
              "palette_name": data.get("palette_name", ""),
              "extra": data.get("meta", {})},
    )


def write_v2(project: UnifiedProject, path: str | os.PathLike) -> None:
    flat = project.mask.reshape(-1).astype(np.uint8).tobytes()
    data = {
        "magic": "IED2",
        "version": 2,
        "project_id": project.project_id,
        "width": int(project.mask.shape[1]),
        "height": int(project.mask.shape[0]),
        "image_path": project.image_path,
        "style_ref_path": project.style_ref_path,
        "mask_base64": base64.b64encode(flat).decode("ascii"),
        "palette_name": project.meta.get("palette_name",
                                         f"{project.project_id}_palette"),
        "style": project.style.to_dict(),
        "meta": project.meta.get("extra", {}),
    }
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def detect_version(path: str | os.PathLike) -> int:
    with open(path, "rb") as fh:
        head = fh.read(4)
    if head == MAGIC_V1:
        return 1
    text = head.lstrip()
    if text.startswith(b"{"):
        return 2
    raise FormatInvalid("无法识别的工程文件头",
                        context={"head": repr(head), "source_path": str(path)})


def parse_project(path: str | os.PathLike) -> UnifiedProject:
    version = detect_version(path)
    if version == 1:
        return parse_v1(path)
    if version == 2:
        return parse_v2(path)
    raise VersionUnsupported(f"未知版本号: {version}",
                             context={"version": version})
