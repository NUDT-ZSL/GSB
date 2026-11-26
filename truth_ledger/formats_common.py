from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import LedgerError
from .hashing import read_json
from .parsing import read_version


def load_json_document(path: str | Path, *, stage: str) -> Any:
    path = Path(path)
    if not path.exists():
        raise LedgerError(
            "E_FIELD_MISSING",
            f"ledger file does not exist: {path}",
            stage=stage,
            details={"path": str(path), "field": "document"},
        )
    try:
        return read_json(path)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            "E_PARSE",
            f"ledger JSON cannot be parsed: {exc.msg}",
            stage=stage,
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    except OSError as exc:
        raise LedgerError(
            "E_PARSE",
            f"ledger file cannot be read: {exc}",
            stage=stage,
            details={"path": str(path)},
        ) from exc


def ensure_reader_version(data: Any, expected: int, *, stage: str) -> int:
    version = read_version(data, stage=stage)
    if version != expected:
        raise LedgerError(
            "E_VERSION_UNSUPPORTED",
            f"{stage} reader received version {version}, expected {expected}",
            stage=stage,
            details={"version": version, "expected": expected},
        )
    return version
