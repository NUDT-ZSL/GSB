"""Deterministic array, PNG, hashing and JSON helpers."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

import numpy as np

from .errors import FusionError


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def array_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    digest = hashlib.sha256()
    digest.update(str(contiguous.dtype).encode("ascii"))
    digest.update(b"|")
    digest.update(",".join(str(v) for v in contiguous.shape).encode("ascii"))
    digest.update(b"|")
    digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def array_descriptor(array: np.ndarray) -> dict[str, Any]:
    return {
        "shape": list(array.shape),
        "dtype": str(array.dtype),
        "sha256": array_sha256(array),
    }


def _atomic_replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def save_npy(path: str | os.PathLike[str], array: np.ndarray) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    try:
        with open(temp_name, "wb") as handle:
            np.save(handle, array, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def save_json(path: str | os.PathLike[str], data: Any) -> None:
    payload = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_replace_bytes(Path(path), payload)


def load_npy(path: str | os.PathLike[str]) -> np.ndarray:
    try:
        loaded = np.load(path, allow_pickle=False)
    except Exception as exc:
        raise FusionError(
            "E_IMAGE_IO",
            f"无法读取 NumPy 数组: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    if isinstance(loaded, np.lib.npyio.NpzFile):
        try:
            keys = list(loaded.files)
            if not keys:
                raise ValueError("npz 文件不包含数组")
            array = loaded[keys[0]]
        finally:
            loaded.close()
        return array
    return np.asarray(loaded)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def write_png(path: str | os.PathLike[str], image: np.ndarray) -> None:
    """Write an 8-bit grayscale, RGB or RGBA PNG without external libraries."""

    array = np.asarray(image)
    if array.dtype != np.uint8:
        if np.issubdtype(array.dtype, np.floating):
            array = np.rint(np.clip(array, 0, 255)).astype(np.uint8)
        else:
            array = np.clip(array, 0, 255).astype(np.uint8)

    if array.ndim == 2:
        color_type = 0
        channels = 1
    elif array.ndim == 3 and array.shape[2] in (3, 4):
        channels = int(array.shape[2])
        color_type = 2 if channels == 3 else 6
    else:
        raise ValueError(f"unsupported PNG array shape: {array.shape}")

    height, width = array.shape[:2]
    raw_rows = bytearray()
    for row in np.ascontiguousarray(array).reshape(height, -1):
        raw_rows.append(0)
        raw_rows.extend(row.tobytes())

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    payload = (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw_rows), level=9))
        + _png_chunk(b"IEND", b"")
    )
    _atomic_replace_bytes(Path(path), payload)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def read_png(path: str | os.PathLike[str]) -> np.ndarray:
    """Read a non-interlaced 8-bit grayscale/RGB/RGBA PNG."""

    try:
        payload = Path(path).read_bytes()
    except OSError as exc:
        raise FusionError(
            "E_IMAGE_IO", f"无法读取 PNG: {path}", {"path": str(path), "reason": str(exc)}
        ) from exc
    if not payload.startswith(PNG_SIGNATURE):
        raise FusionError("E_IMAGE_IO", f"不是有效 PNG: {path}", {"path": str(path)})

    offset = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = 0
    interlace = 0
    idat = bytearray()
    while offset + 12 <= len(payload):
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        chunk = payload[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if compression != 0 or filtering != 0:
                raise FusionError("E_IMAGE_IO", "PNG 使用了不支持的压缩/滤波方法")
        elif kind == b"IDAT":
            idat.extend(chunk)
        elif kind == b"IEND":
            break

    if bit_depth != 8 or interlace != 0 or color_type not in (0, 2, 4, 6):
        raise FusionError(
            "E_IMAGE_IO",
            "仅支持非隔行、8 位灰度/RGB/RGBA PNG",
            {"bit_depth": bit_depth, "color_type": color_type, "interlace": interlace},
        )
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    try:
        decompressed = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise FusionError("E_IMAGE_IO", "PNG IDAT 解压失败", {"reason": str(exc)}) from exc

    stride = width * channels
    expected = height * (stride + 1)
    if len(decompressed) != expected:
        raise FusionError(
            "E_IMAGE_IO",
            "PNG 解压后数据长度不符合 IHDR",
            {"expected": expected, "actual": len(decompressed)},
        )

    output = np.empty((height, width, channels), dtype=np.uint8)
    previous = np.zeros(stride, dtype=np.int16)
    cursor = 0
    for row_index in range(height):
        filter_type = decompressed[cursor]
        cursor += 1
        scanline = np.frombuffer(decompressed[cursor : cursor + stride], dtype=np.uint8).astype(np.int16)
        cursor += stride
        reconstructed = np.empty(stride, dtype=np.int16)
        for index in range(stride):
            left = int(reconstructed[index - channels]) if index >= channels else 0
            up = int(previous[index])
            up_left = int(previous[index - channels]) if index >= channels else 0
            value = int(scanline[index])
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                predictor = _paeth(left, up, up_left)
            else:
                raise FusionError("E_IMAGE_IO", f"不支持的 PNG 行滤波类型: {filter_type}")
            reconstructed[index] = (value + predictor) & 0xFF
        output[row_index] = reconstructed.reshape(width, channels).astype(np.uint8)
        previous = reconstructed

    if channels == 2:
        return output
    if channels == 1:
        return output[:, :, 0]
    return output


def load_image(path: str | os.PathLike[str]) -> np.ndarray:
    suffix = Path(path).suffix.lower()
    if suffix in {".npy", ".npz"}:
        return load_npy(path)
    if suffix == ".png":
        return read_png(path)
    raise FusionError(
        "E_IMAGE_IO",
        f"不支持的图像格式: {suffix or '<none>'}（支持 .npy/.npz/.png）",
        {"path": str(path)},
    )


def _normalize_image_values(array: np.ndarray) -> np.ndarray:
    if array.dtype == np.bool_:
        return array.astype(np.float64) * 255.0
    if np.issubdtype(array.dtype, np.integer):
        if array.dtype == np.uint8:
            return array.astype(np.float64)
        info = np.iinfo(array.dtype)
        if info.max <= 255:
            return array.astype(np.float64)
        return array.astype(np.float64) * (255.0 / float(info.max))
    values = array.astype(np.float64)
    finite = values[np.isfinite(values)]
    if finite.size and float(np.min(finite)) >= -1e-9 and float(np.max(finite)) <= 1.0 + 1e-9:
        values = values * 255.0
    return values


def image_to_rgb_or_rgba(array: np.ndarray, name: str = "image") -> np.ndarray:
    normalized = _normalize_image_values(np.asarray(array))
    if normalized.ndim == 2:
        normalized = np.repeat(normalized[:, :, None], 3, axis=2)
    elif normalized.ndim == 3 and normalized.shape[2] == 1:
        normalized = np.repeat(normalized, 3, axis=2)
    elif normalized.ndim == 3 and normalized.shape[2] == 2:
        gray_alpha = normalized
        normalized = np.dstack(
            (
                gray_alpha[:, :, 0],
                gray_alpha[:, :, 0],
                gray_alpha[:, :, 0],
                gray_alpha[:, :, 1],
            )
        )
    elif normalized.ndim == 3 and normalized.shape[2] not in (3, 4):
        raise FusionError(
            "E_INVALID_IMAGE",
            f"{name} 必须为灰度、RGB 或 RGBA 图像",
            {"shape": list(normalized.shape)},
        )
    if not np.all(np.isfinite(normalized)):
        location = np.argwhere(~np.isfinite(normalized))
        y, x = int(location[0, 0]), int(location[0, 1])
        raise FusionError(
            "E_NONFINITE_INPUT",
            f"{name} 包含非有限值",
            {"array": name, "first_pixel": [y, x], "shape": list(normalized.shape)},
        )
    return normalized


def mask_from_array(array: np.ndarray) -> np.ndarray:
    values = np.asarray(array)
    if values.ndim == 3 and values.shape[2] in (1, 3, 4):
        if values.shape[2] in (3, 4):
            rgb = values[:, :, :3]
            if not (np.array_equal(rgb[:, :, 0], rgb[:, :, 1]) and np.array_equal(rgb[:, :, 1], rgb[:, :, 2])):
                values = np.rint(
                    0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
                )
            else:
                values = rgb[:, :, 0]
        else:
            values = values[:, :, 0]
    if values.ndim != 2:
        raise FusionError(
            "E_MASK_SHAPE",
            "掩码必须为二维数组或单通道图像",
            {"shape": list(values.shape)},
        )
    if np.issubdtype(values.dtype, np.floating):
        if not np.all(np.isfinite(values)):
            location = np.argwhere(~np.isfinite(values))
            y, x = int(location[0, 0]), int(location[0, 1])
            raise FusionError(
                "E_NONFINITE_INPUT",
                "掩码包含非有限值",
                {"array": "mask", "first_pixel": [y, x]},
            )
    return values > 0


def load_mask(path: str | os.PathLike[str]) -> np.ndarray:
    return mask_from_array(load_image(path))


def append_jsonl(path: str | os.PathLike[str], record: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(destination, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


