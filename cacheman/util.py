"""Validation, canonical serialization, hashing, and path helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .errors import CACHE_MANIFEST_INVALID, CacheError


_TASK_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA256 = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


class DuplicateJsonKey(ValueError):
    pass


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def object_digest(value: Any) -> str:
    return "sha256:" + sha256_text(canonical_json(value))


def load_json_file(path: Path, *, code: str = CACHE_MANIFEST_INVALID) -> tuple[dict[str, Any], bytes]:
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise CacheError(
            code,
            "manifest file does not exist",
            fields={"path": str(path)},
        )
    except OSError as exc:
        raise CacheError(
            code,
            "manifest file cannot be read",
            fields={"path": str(path), "reason": str(exc)},
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CacheError(
            code,
            "manifest is not valid UTF-8",
            fields={"path": str(path), "offset": exc.start},
        )
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs)
    except DuplicateJsonKey as exc:
        raise CacheError(
            code,
            "manifest contains a duplicate JSON object key",
            fields={"path": str(path), "key": str(exc)},
        )
    except json.JSONDecodeError as exc:
        raise CacheError(
            code,
            "manifest is not valid JSON",
            fields={
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "position": exc.pos,
            },
        )
    if not isinstance(value, dict):
        raise CacheError(
            code,
            "manifest root must be a JSON object",
            fields={"path": str(path), "actual_type": type(value).__name__},
        )
    return value, raw


def require_object(value: Any, field: str, *, code: str = CACHE_MANIFEST_INVALID) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CacheError(
            code,
            f"{field} must be an object",
            fields={"field": field, "actual_type": type(value).__name__},
        )
    return value


def require_list(value: Any, field: str, *, code: str = CACHE_MANIFEST_INVALID) -> list[Any]:
    if not isinstance(value, list):
        raise CacheError(
            code,
            f"{field} must be an array",
            fields={"field": field, "actual_type": type(value).__name__},
        )
    return value


def require_str(
    value: Any,
    field: str,
    *,
    nonempty: bool = True,
    code: str = CACHE_MANIFEST_INVALID,
) -> str:
    if not isinstance(value, str):
        raise CacheError(
            code,
            f"{field} must be a string",
            fields={"field": field, "actual_type": type(value).__name__},
        )
    if nonempty and not value.strip():
        raise CacheError(
            code,
            f"{field} must not be empty",
            fields={"field": field},
        )
    return value


def require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    code: str = CACHE_MANIFEST_INVALID,
) -> int:
    if type(value) is not int:
        raise CacheError(
            code,
            f"{field} must be an integer",
            fields={"field": field, "actual_type": type(value).__name__},
        )
    if minimum is not None and value < minimum:
        raise CacheError(
            code,
            f"{field} must be at least {minimum}",
            fields={"field": field, "value": value, "minimum": minimum},
        )
    return value


def require_exact_keys(
    value: dict[str, Any],
    required: Iterable[str],
    *,
    optional: Iterable[str] = (),
    field: str = "object",
    code: str = CACHE_MANIFEST_INVALID,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise CacheError(
            code,
            f"{field} has an invalid field set",
            fields={"field": field, "missing": missing, "unexpected": extra},
        )


def canonical_task_key(
    raw: Any,
    *,
    field: str = "task",
    code: str = CACHE_MANIFEST_INVALID,
) -> str:
    text = require_str(raw, field, code=code).strip()
    text = unicodedata.normalize("NFC", text).replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if text.startswith("/") or text.endswith("/"):
        raise CacheError(
            code,
            f"{field} is not a canonical task key",
            fields={"field": field, "value": raw, "reason": "leading_or_trailing_slash"},
        )
    parts = text.split("/")
    if not parts or any(part in {"", ".", ".."} or not _TASK_SEGMENT.match(part) for part in parts):
        raise CacheError(
            code,
            f"{field} is not a canonical task key",
            fields={"field": field, "value": raw, "reason": "invalid_segment"},
        )
    return text


def require_canonical_task_key(
    raw: Any,
    *,
    field: str = "task_key",
    code: str = CACHE_MANIFEST_INVALID,
) -> str:
    canonical = canonical_task_key(raw, field=field, code=code)
    if raw != canonical:
        raise CacheError(
            code,
            f"{field} must already be in canonical form",
            fields={"field": field, "value": raw, "canonical": canonical},
        )
    return canonical


def artifact_name(
    raw: Any,
    *,
    field: str = "artifact",
    code: str = CACHE_MANIFEST_INVALID,
) -> str:
    text = require_str(raw, field, code=code)
    if not _ARTIFACT_NAME.match(text):
        raise CacheError(
            code,
            f"{field} is not a valid artifact name",
            fields={"field": field, "value": text},
        )
    return text


def normalize_sha256(raw: Any, *, field: str = "sha256") -> str:
    text = require_str(raw, field)
    match = _SHA256.match(text)
    if not match:
        raise CacheError(
            CACHE_MANIFEST_INVALID,
            f"{field} must be a 64-digit SHA-256 digest",
            fields={"field": field, "value": text},
        )
    return "sha256:" + match.group(1).lower()


def digest_hex(digest: str) -> str:
    return digest.split(":", 1)[1]


def display_path(path: Path, base: Path | None = None) -> str:
    candidate = Path(path)
    if base is not None:
        try:
            return candidate.resolve().relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            pass
    return candidate.as_posix()


def resolve_content_path(root: Path, raw: str, *, field: str) -> Path:
    require_str(raw, field)
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    return path
