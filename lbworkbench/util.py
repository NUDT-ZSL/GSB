from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

from .errors import WorkbenchError, bad_snapshot


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
    ) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def canonical_instance_id(value: Any, field_name: str = "instance_id") -> str:
    if not isinstance(value, str):
        raise bad_snapshot(f"{field_name} must be a string", field=field_name)
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    if not normalized:
        raise bad_snapshot(f"{field_name} must not be empty", field=field_name)
    if len(normalized) > 256:
        raise bad_snapshot(f"{field_name} is too long", field=field_name)
    return normalized


def require_int(
    value: Any,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_snapshot(f"{field_name} must be an integer", field=field_name)
    if minimum is not None and value < minimum:
        raise bad_snapshot(
            f"{field_name} must be >= {minimum}",
            field=field_name,
            value=value,
        )
    return value


def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise bad_snapshot(f"{field_name} must be a boolean", field=field_name)
    return value


def require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise bad_snapshot(f"{field_name} must be a non-empty string", field=field_name)
    return value


def load_json_file(path: Path, *, error_code: str = "E_BAD_SNAPSHOT") -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise WorkbenchError(
            error_code,
            f"cannot read file: {path}",
            {"path": str(path), "os_error": exc.strerror or str(exc)},
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkbenchError(
            error_code,
            f"invalid JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def safe_resolve(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise bad_snapshot("package file path must be a non-empty string")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise bad_snapshot(
            "package path escapes package root",
            path=relative,
            package_root=str(root),
        ) from exc
    return candidate


_DOTTED_NUMBER = re.compile(r"^\d+(?:\.\d+)*$")


def version_key(version: str) -> tuple[Any, ...]:
    if _DOTTED_NUMBER.fullmatch(version):
        parts = [int(part) for part in version.split(".")]
        width = max(len(parts), 8)
        return (1, tuple(parts + [0] * (width - len(parts))))
    return (0, version)


def compare_versions(left: str, right: str) -> int:
    left_key = version_key(left)
    right_key = version_key(right)
    if left_key[0] != right_key[0]:
        # Numeric versions sort after opaque labels.
        return 1 if left_key[0] > right_key[0] else -1
    if left_key == right_key:
        return 0
    return 1 if left_key > right_key else -1


def ensure_workdir(path: str | os.PathLike[str]) -> Path:
    workdir = Path(path).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if not os.access(workdir, os.W_OK):
        raise WorkbenchError(
            "E_WORKDIR_NOT_WRITABLE",
            "work directory is not writable",
            {"workdir": str(workdir)},
            http_status=500,
        )
    return workdir


def status_rank(health: str) -> int:
    return {"healthy": 0, "suspect": 1, "unhealthy": 2}.get(health, 3)


def weight_for_health(base_weight: int, health: str) -> int:
    if health == "unhealthy":
        return 0
    if health == "suspect":
        return 0 if base_weight == 0 else max(1, base_weight // 2)
    return base_weight
