from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def json_bytes(data: Any) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: str | Path, data: Any) -> None:
    atomic_write_bytes(path, json_bytes(data))


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def array_sha256(array: Any) -> str:
    import numpy as np

    return sha256_bytes(np.ascontiguousarray(array).tobytes(order="C"))


def deterministic_npz_bytes(arrays: dict[str, Any]) -> bytes:
    """Create an NPZ with fixed ZIP metadata so replay bytes are reproducible."""
    import numpy as np

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for name in sorted(arrays):
            array_buffer = io.BytesIO()
            np.lib.format.write_array(
                array_buffer,
                np.ascontiguousarray(arrays[name]),
                allow_pickle=False,
            )
            information = zipfile.ZipInfo(
                f"{name}.npy",
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            information.compress_type = zipfile.ZIP_DEFLATED
            information.external_attr = 0o600 << 16
            archive.writestr(information, array_buffer.getvalue())
    return buffer.getvalue()
