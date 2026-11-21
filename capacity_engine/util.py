from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterator

from .errors import BizError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=json_default,
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        default=json_default,
    )


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_str(value)
    if isinstance(value, Fraction):
        return fraction_str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def decimal_str(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return format(value.normalize(), "f")


def fraction_str(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def fraction_dict(value: Fraction) -> dict[str, Any]:
    return {
        "num": value.numerator,
        "den": value.denominator,
        "decimal": decimal_from_fraction(value),
    }


def decimal_from_fraction(value: Fraction, digits: int = 18) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    with localcontext_precision(digits + 8):
        dec = Decimal(value.numerator) / Decimal(value.denominator)
    return format(dec, f".{digits}f").rstrip("0").rstrip(".") or "0"


class localcontext_precision:
    def __init__(self, precision: int) -> None:
        self.precision = precision
        self._ctx = None

    def __enter__(self):
        from decimal import localcontext

        self._ctx = localcontext()
        ctx = self._ctx.__enter__()
        ctx.prec = self.precision
        return ctx

    def __exit__(self, exc_type, exc, tb):
        return self._ctx.__exit__(exc_type, exc, tb)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BizError(
            "E_SCENE_SHAPE",
            f"{field} must be a JSON object",
            details={"field": field},
        )
    return value


def require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise BizError(
            "E_SCENE_SHAPE",
            f"{field} must be a JSON array",
            details={"field": field},
        )
    return value


def parse_decimal(value: Any, field: str, code: str = "E_INVALID_FIELD") -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BizError(code, f"{field} must be a finite decimal", details={"field": field})
    try:
        if isinstance(value, Decimal):
            result = value
        else:
            result = Decimal(str(value))
    except Exception as exc:
        raise BizError(
            code,
            f"{field} must be a finite decimal",
            details={"field": field, "value": str(value), "reason": str(exc)},
        ) from exc
    if not result.is_finite():
        raise BizError(
            code,
            f"{field} must be a finite decimal",
            details={"field": field, "value": str(value)},
        )
    return result


def parse_positive_int(value: Any, field: str, code: str = "E_INVALID_FIELD") -> int:
    if not is_int(value) or value <= 0:
        raise BizError(
            code,
            f"{field} must be a positive integer",
            details={"field": field, "value": value},
        )
    return value


def parse_nonnegative_int(value: Any, field: str, code: str = "E_INVALID_FIELD") -> int:
    if not is_int(value) or value < 0:
        raise BizError(
            code,
            f"{field} must be a non-negative integer",
            details={"field": field, "value": value},
        )
    return value


def parse_bool(value: Any, field: str, code: str = "E_INVALID_FIELD") -> bool:
    if not isinstance(value, bool):
        raise BizError(
            code,
            f"{field} must be a boolean",
            details={"field": field, "value": value},
        )
    return value


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            self._handle.write(b"\0")
            self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is None:
            return False
        if os.name == "nt":
            import msvcrt

            self._handle.seek(0)
            try:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
        return False


def chunks(seq: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(seq), size):
        yield seq[index : index + size]

