from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np


def write_json(path: str | Path, value: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def write_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_pnm(path: str | Path, image_u8: np.ndarray) -> Path:
    """Write a binary PPM (RGB) or PGM (grayscale) image without Pillow."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.ascontiguousarray(image_u8, dtype=np.uint8)
    height, width = array.shape[:2]
    if array.ndim == 2 or array.shape[2] == 1:
        if array.ndim == 3:
            array = array[:, :, 0]
        header = f"P5\n{width} {height}\n255\n".encode("ascii")
        path = path if path.suffix else path.with_suffix(".pgm")
        path.write_bytes(header + array.tobytes(order="C"))
    else:
        if array.shape[2] != 3:
            raise ValueError("PPM output only supports three channels")
        header = f"P6\n{width} {height}\n255\n".encode("ascii")
        path = path if path.suffix else path.with_suffix(".ppm")
        path.write_bytes(header + array.tobytes(order="C"))
    return path


def save_image_bundle(stem: str | Path, image: np.ndarray, image_u8: np.ndarray) -> dict[str, str]:
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    npy_path = stem.with_suffix(".npy")
    np.save(npy_path, image)
    pnm_path = write_pnm(stem, image_u8)
    return {"npy": str(npy_path), "preview": str(pnm_path)}


def load_reference(path: str | Path) -> np.ndarray:
    path = Path(path)
    if path.suffix == ".npy":
        return np.load(path)
    if path.suffix.lower() in (".pgm", ".ppm"):
        data = path.read_bytes()
        tokens = []
        index = 0
        while len(tokens) < 4:
            while index < len(data) and data[index] in b" \t\r\n":
                index += 1
            if data[index:index + 1] == b"#":
                while index < len(data) and data[index] not in b"\r\n":
                    index += 1
                continue
            start = index
            while index < len(data) and data[index] not in b" \t\r\n":
                index += 1
            tokens.append(data[start:index])
        magic = tokens[0]
        width, height, max_value = (int(token) for token in tokens[1:4])
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1
        raw = data[index:]
        if magic == b"P5":
            return np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
        if magic == b"P6":
            return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
        raise ValueError(f"unsupported PNM magic: {magic!r}")
    raise ValueError(f"unsupported reference format: {path.suffix}")
