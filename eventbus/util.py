"""Canonical encoding, deterministic arithmetic, and cross-platform locking."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from .errors import err


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_value(value: Any) -> str:
    return sha256_text(canonical_json(value))


def payload_digest(fields: dict[str, Any]) -> str:
    return digest_value(fields)


def read_json(path: str | Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise err(
            "INVALID_JSON",
            f"JSON 解析失败: {path}",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    except OSError as exc:
        raise err("NOT_FOUND", f"无法读取文件: {path}", path=str(path), reason=str(exc)) from exc


def ensure_json_value(value: Any, *, path: str = "value") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise err("MALFORMED_EVENT", f"{path} 包含非有限浮点数", path=path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            ensure_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise err("MALFORMED_EVENT", f"{path} 的对象键必须为字符串", path=path)
            ensure_json_value(item, path=f"{path}.{key}")
        return
    raise err("MALFORMED_EVENT", f"{path} 不是合法 JSON 标量/容器", path=path)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise err("MALFORMED_EVENT", "数值变换输入不是有限数字", value=str(value))
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise err(
            "MALFORMED_EVENT",
            "数值变换只接受数字或数字字符串",
            value=value,
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise err("MALFORMED_EVENT", "数值变换输入不是合法数字", value=value) from exc
    if not number.is_finite():
        raise err("MALFORMED_EVENT", "数值变换输入不是有限数字", value=value)
    return number


def deterministic_number(value: Any, scale: str | None = None) -> int | float:
    number = to_decimal(value)
    if scale is not None:
        if not is_int(scale) or scale < 0:
            raise err("MALFORMED_EVENT", "transform scale 必须为非负整数", scale=scale)
        quantum = Decimal(1).scaleb(-scale)
        number = number.quantize(quantum, rounding=ROUND_HALF_UP)
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def typed_value(value: Any, type_name: str) -> Any:
    if type_name == "string":
        if isinstance(value, str):
            return value
        raise err("MALFORMED_EVENT", "字段类型必须为 string", value=value)
    if type_name == "number":
        return deterministic_number(value)
    if type_name == "integer":
        number = to_decimal(value)
        if number != number.to_integral_value():
            raise err("MALFORMED_EVENT", "字段类型必须为 integer", value=value)
        return int(number)
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        raise err("MALFORMED_EVENT", "字段类型必须为 boolean", value=value)
    if type_name == "object":
        if isinstance(value, dict):
            return value
        raise err("MALFORMED_EVENT", "字段类型必须为 object", value=value)
    if type_name == "any":
        ensure_json_value(value)
        return value
    raise err("MALFORMED_EVENT", "未知字段类型", type_name=type_name)


def now_ms() -> int:
    return int(time.time() * 1000)


@contextlib.contextmanager
def file_lock(path: str | Path, timeout: float = 30.0) -> Iterator[None]:
    """Advisory lock used together with SQLite for cross-process writers."""

    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise err(
                        "CONCURRENT_MODIFICATION",
                        "等待数据库写锁超时",
                        lock_path=str(lock_path),
                    )
                time.sleep(0.01)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
