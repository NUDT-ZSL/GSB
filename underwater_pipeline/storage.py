"""Deterministic, atomic NPZ persistence."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import numpy as np


def _npy_bytes(value: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.lib.format.write_array(buffer, np.asarray(value), allow_pickle=False)
    return buffer.getvalue()


def save_npz_deterministic(
    path: str | Path,
    arrays: dict[str, np.ndarray],
) -> Path:
    """Write a compressed NPZ with fixed ZIP metadata for byte-stable replay."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with zipfile.ZipFile(
        temporary, mode="w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for name, value in arrays.items():
            info = zipfile.ZipInfo(
                filename=f"{name}.npy",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, _npy_bytes(value))
    os.replace(temporary, destination)
    return destination
