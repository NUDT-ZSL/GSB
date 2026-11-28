"""Filesystem and JSON helpers shared by generation and pipeline stages."""

from __future__ import annotations

import json
import io
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import numpy as np


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        from .errors import fail

        fail("E_INPUT_MISSING", "required JSON file does not exist", path=str(path))
    except json.JSONDecodeError as exc:
        from .errors import fail

        fail(
            "E_JSON_INVALID",
            f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
            path=str(path),
        )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def replace_directory(staged: Path, destination: Path) -> None:
    """Replace a generated directory while preserving the old copy on failure."""
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}.backup-", dir=str(destination.parent)
            )
        )
        backup.rmdir()
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup is not None and backup.exists():
            os.replace(backup, destination)
        raise
    if backup is not None and backup.exists():
        shutil.rmtree(backup)


def prepare_staging(destination: Path) -> Path:
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=str(destination.parent))
    )


def write_npz(path: Path, **arrays: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, array in arrays.items():
            buffer = io.BytesIO()
            np.lib.format.write_array(
                buffer,
                np.asanyarray(array),
                allow_pickle=False,
            )
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, buffer.getvalue())


def clean_float(value: float, digits: int = 9) -> float:
    rounded = round(float(value), digits)
    if abs(rounded) < 0.5 * 10 ** (-digits):
        return 0.0
    return rounded


def clean_nested(value: Any, digits: int = 9) -> Any:
    if isinstance(value, float):
        return clean_float(value, digits)
    if isinstance(value, dict):
        return {key: clean_nested(item, digits) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_nested(item, digits) for item in value]
    if isinstance(value, np.generic):
        return clean_nested(value.item(), digits)
    return value
