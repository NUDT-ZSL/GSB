from __future__ import annotations

import binascii
import struct
import zlib
from pathlib import Path

import numpy as np

from .errors import BusinessError, ErrorCode


def ensure_rgb_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    if frame.dtype != np.uint8 or frame.shape != (height, width, 3):
        raise BusinessError(
            ErrorCode.E_ANCHOR_MISMATCH,
            "参考帧格式与配置不匹配",
            {
                "expected_shape": [height, width, 3],
                "actual_shape": list(frame.shape),
                "expected_dtype": "uint8",
                "actual_dtype": str(frame.dtype),
            },
        )
    return np.ascontiguousarray(frame)


def load_ppm(path: Path, width: int, height: int) -> np.ndarray:
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise BusinessError(
            ErrorCode.E_ANCHOR_MISMATCH,
            "本地锚点图片必须为 P6 PPM 或原始 RGB 文件",
            {"path": str(path)},
        )
    tokens: list[bytes] = []
    index = 2
    while len(tokens) < 3 and index < len(data):
        while index < len(data) and data[index : index + 1].isspace():
            index += 1
        if index < len(data) and data[index : index + 1] == b"#":
            newline = data.find(b"\n", index)
            index = len(data) if newline < 0 else newline + 1
            continue
        start = index
        while index < len(data) and not data[index : index + 1].isspace():
            index += 1
        tokens.append(data[start:index])
    if len(tokens) != 3:
        raise BusinessError(ErrorCode.E_ANCHOR_MISMATCH, "PPM 头部不完整", {"path": str(path)})
    try:
        ppm_width, ppm_height, max_value = (int(token) for token in tokens)
    except ValueError as exc:
        raise BusinessError(ErrorCode.E_ANCHOR_MISMATCH, "PPM 头部数值无效", {"path": str(path)}) from exc
    index += 1
    if max_value != 255 or ppm_width != width or ppm_height != height:
        raise BusinessError(
            ErrorCode.E_ANCHOR_MISMATCH,
            "PPM 尺寸或量化范围与配置不匹配",
            {
                "path": str(path),
                "expected": [width, height, 255],
                "actual": [ppm_width, ppm_height, max_value],
            },
        )
    pixels = data[index:]
    if len(pixels) != width * height * 3:
        raise BusinessError(
            ErrorCode.E_ANCHOR_MISMATCH,
            "PPM 像素数据长度不匹配",
            {"expected_bytes": width * height * 3, "actual_bytes": len(pixels)},
        )
    return np.frombuffer(pixels, dtype=np.uint8).reshape(height, width, 3).copy()


def load_anchor(path: Path, width: int, height: int) -> np.ndarray:
    suffix = path.suffix.lower()
    if suffix in {".ppm", ".pnm"}:
        return ensure_rgb_frame(load_ppm(path, width, height), width, height)
    if suffix in {".rgb", ".raw"}:
        data = path.read_bytes()
        expected = width * height * 3
        if len(data) != expected:
            raise BusinessError(
                ErrorCode.E_ANCHOR_MISMATCH,
                "原始 RGB 锚点文件长度不匹配",
                {"expected_bytes": expected, "actual_bytes": len(data), "path": str(path)},
            )
        return np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3).copy()
    raise BusinessError(
        ErrorCode.E_ANCHOR_MISMATCH,
        "不支持的本地锚点图片格式",
        {"path": str(path), "supported": [".ppm", ".pnm", ".rgb", ".raw"]},
    )


def save_ppm(path: Path, frame: np.ndarray) -> None:
    frame = np.ascontiguousarray(frame, dtype=np.uint8)
    height, width, channels = frame.shape
    if channels != 3:
        raise ValueError("PPM writer requires RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"P6\n%d %d\n255\n" % (width, height) + frame.tobytes())


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(frame: np.ndarray) -> bytes:
    """Encode an RGB uint8 array without third-party imaging dependencies."""
    frame = np.ascontiguousarray(frame, dtype=np.uint8)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("PNG encoder requires HxWx3 RGB")
    height, width, _ = frame.shape
    scanlines = b"".join(b"\x00" + frame[row].tobytes() for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
        + _png_chunk(b"IEND", b"")
    )


def make_reference_frame(width: int, height: int, seed: int = 20261120) -> np.ndarray:
    """Generate a deterministic local reference image with no external assets."""
    y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    phase = np.float32((seed % 997) / 997.0 * np.pi)
    ridge = np.exp(-((y - 0.48 - 0.10 * np.sin(x * 8.0 + phase)) ** 2) / 0.018)
    glow = np.exp(-((x - 0.70) ** 2 + (y - 0.28) ** 2) / 0.055)
    red = 38 + 150 * ridge + 95 * glow + 25 * (1.0 - y)
    green = 46 + 56 * ridge + 42 * glow + 20 * x
    blue = 68 + 34 * ridge + 20 * glow + 45 * y
    rgb = np.stack([red, green, blue], axis=-1)
    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
