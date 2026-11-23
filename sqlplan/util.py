from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Union


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_object(value: Any) -> str:
    return sha256_text(canonical_json(value))


def normalize_digest(value: str) -> str:
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text


def is_sha256(value: str) -> bool:
    text = normalize_digest(value)
    return len(text) == 64 and all(ch in "0123456789abcdef" for ch in text)


def read_json_file(path: Union[str, Path]) -> Any:
    path = Path(path)

    def reject_nonfinite(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=reject_nonfinite)
    except FileNotFoundError as exc:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "JSON 文件不存在",
            {"path": str(path)},
        ) from exc
    except OSError as exc:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "JSON 文件无法读取",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    except UnicodeDecodeError as exc:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "JSON 文件不是 UTF-8 编码",
            {
                "path": str(path),
                "offset": exc.start,
                "reason": exc.reason,
            },
        ) from exc
    except ValueError as exc:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "JSON 包含非法数值或结构",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        from .errors import BusinessError, ErrorCode

        raise BusinessError(
            ErrorCode.E_BAD_JSON,
            "JSON 文件无法解析",
            {
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc


def ensure_parent(path: Union[str, Path]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_bytes(path: Union[str, Path], data: bytes) -> None:
    target = ensure_parent(path)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def atomic_write_json(path: Union[str, Path], value: Any) -> None:
    atomic_write_bytes(path, pretty_json(value).encode("utf-8") + b"\n")


def stable_columns(columns: Any) -> list:
    if columns is None:
        return []
    if isinstance(columns, dict):
        return [dict(value) for _, value in sorted(columns.items())]
    return list(columns)
