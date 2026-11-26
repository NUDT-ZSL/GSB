from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import LedgerError
from .format_v1 import read_v1, write_v1
from .format_v2 import MANIFEST_NAME, read_v2, write_v2
from .format_v3 import read_v3, write_v3
from .formats_common import load_json_document
from .model import CSRMatrix
from .parsing import read_version


def read_ledger(version: int, path: str | Path) -> tuple[CSRMatrix, dict[str, Any]]:
    if version == 1:
        return read_v1(path)
    if version == 2:
        return read_v2(path)
    if version == 3:
        return read_v3(path)
    raise LedgerError(
        "E_VERSION_UNSUPPORTED",
        f"unsupported ledger version {version}",
        stage="format_dispatch",
        details={"version": version},
    )


def read_from_path(path: str | Path) -> tuple[int, CSRMatrix, dict[str, Any]]:
    target = Path(path)
    if target.is_dir() or (target / MANIFEST_NAME).exists():
        matrix, info = read_v2(target)
        return 2, matrix, info
    data = load_json_document(target, stage="format_dispatch")
    version = read_version(data, stage="format_dispatch")
    matrix, info = read_ledger(version, target)
    return version, matrix, info


def write_ledger(
    version: int,
    path: str | Path,
    matrix: CSRMatrix,
    **kwargs: Any,
) -> Path:
    if version == 1:
        return write_v1(path, matrix)
    if version == 2:
        return write_v2(path, matrix, **kwargs)
    if version == 3:
        return write_v3(path, matrix, **kwargs)
    raise LedgerError(
        "E_VERSION_UNSUPPORTED",
        f"unsupported target ledger version {version}",
        stage="migrate_semantic",
        details={"version": version},
    )
