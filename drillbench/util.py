from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .errors import BusinessError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def digest_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def stable_json(value: Any, *, indent: int = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json_file(path: Path, error_code: str) -> tuple[Any, bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BusinessError(
            error_code,
            f"cannot read JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BusinessError(
            error_code,
            f"JSON file is not UTF-8: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BusinessError(
            error_code,
            f"invalid JSON in {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            error_code,
            f"top-level JSON value must be an object: {path}",
            {"path": str(path)},
        )
    return value, raw, sha256_bytes(raw)


def require_dict(
    value: Any,
    *,
    code: str,
    path: str,
    message: str = "expected object",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, f"{path}: {message}", {"path": path})
    return value


def require_list(
    value: Any,
    *,
    code: str,
    path: str,
    message: str = "expected array",
) -> list[Any]:
    if not isinstance(value, list):
        raise BusinessError(code, f"{path}: {message}", {"path": path})
    return value


def require_str(
    value: Any,
    *,
    code: str,
    path: str,
    nonempty: bool = True,
) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise BusinessError(code, f"{path}: expected non-empty string", {"path": path})
    return value


def require_int(
    value: Any,
    *,
    code: str,
    path: str,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(code, f"{path}: expected integer", {"path": path})
    if minimum is not None and value < minimum:
        raise BusinessError(
            code,
            f"{path}: integer must be >= {minimum}",
            {"path": path, "value": value},
        )
    return value


def require_bool(value: Any, *, code: str, path: str) -> bool:
    if not isinstance(value, bool):
        raise BusinessError(code, f"{path}: expected boolean", {"path": path})
    return value


def ensure_within(base: Path, target: Path) -> Path:
    base_abs = base.resolve()
    target_abs = target.resolve()
    try:
        target_abs.relative_to(base_abs)
    except ValueError as exc:
        raise BusinessError(
            "E_PATH",
            "path escapes the workspace root",
            {"base": str(base_abs), "target": str(target_abs)},
        ) from exc
    return target_abs


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            ctypes.windll.kernel32.CloseHandle(process)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

