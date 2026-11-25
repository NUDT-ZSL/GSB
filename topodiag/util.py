"""Small deterministic and transactional helpers."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$")
RESOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TEMPLATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)$"
)


def now_ms() -> int:
    return int(time.time() * 1000)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BusinessError(
            "INPUT_FILE_MISSING",
            f"Required input file is missing: {path}",
            path=str(path),
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BusinessError(
            "INPUT_JSON_INVALID",
            f"Input file is not valid JSON: {path}",
            path=str(path),
            parse_error=str(exc),
        ) from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = pretty_json(value) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def parse_semver(value: Any, *, field: str = "manifest_version") -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise BusinessError(
            "INVALID_MANIFEST_VERSION",
            f"{field} must be a semantic version string",
            version=value,
            field=field,
        )
    match = SEMVER_RE.fullmatch(value)
    if not match:
        raise BusinessError(
            "INVALID_MANIFEST_VERSION",
            f"{field} must use MAJOR.MINOR.PATCH form",
            version=value,
            field=field,
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def compare_semver(left: str, right: str) -> int:
    a = parse_semver(left)
    b = parse_semver(right)
    return (a > b) - (a < b)


def is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_label(label: Any, **diagnostics: Any) -> str:
    if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
        raise BusinessError(
            "INVALID_LABEL",
            f"Invalid label value: {label!r}",
            label=label,
            **diagnostics,
        )
    return label


def validate_labels(labels: Any, **diagnostics: Any) -> tuple[str, ...]:
    if not isinstance(labels, list):
        raise BusinessError(
            "INVALID_LABEL",
            "Template labels must be a JSON array",
            labels=labels,
            **diagnostics,
        )
    checked = [validate_label(item, **diagnostics) for item in labels]
    if len(set(checked)) != len(checked):
        raise BusinessError(
            "INVALID_LABEL",
            "Template labels must be unique",
            labels=checked,
            **diagnostics,
        )
    return tuple(sorted(checked))


@contextlib.contextmanager
def workspace_lock(workdir: Path, name: str = "runtime") -> Iterator[None]:
    """Cross-process advisory lock backed by flock."""

    lock_dir = workdir / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def open_fd(path: Path, flags: int) -> Iterator[int]:
    descriptor = os.open(path, flags)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def append_json_log(
    workdir: Path,
    command: str,
    event: str,
    **fields: Any,
) -> None:
    """Append one JSON-lines audit record; failures never mask the command."""

    try:
        log_dir = workdir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts_ms": now_ms(),
            "command": command,
            "event": event,
            **fields,
        }
        line = canonical_json(record) + "\n"
        descriptor = os.open(
            log_dir / "topodiag.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
        finally:
            os.close(descriptor)
    except OSError:
        return
