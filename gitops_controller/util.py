"""Small deterministic helpers shared by the controller."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import DuplicateKeyError, GitOpsError

APP_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SEMVER_NUMBER = r"(?:0|[1-9]\d*)"
_SEMVER_PRERELEASE_IDENTIFIER = (
    rf"(?:{_SEMVER_NUMBER}|\d*[A-Za-z-][0-9A-Za-z-]*)"
)
_SEMVER_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
SEMVER_RE = re.compile(
    rf"^{_SEMVER_NUMBER}\.{_SEMVER_NUMBER}\.{_SEMVER_NUMBER}"
    rf"(?:-{_SEMVER_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{_SEMVER_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+{_SEMVER_BUILD_IDENTIFIER}"
    rf"(?:\.{_SEMVER_BUILD_IDENTIFIER})*)?$"
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def read_json_file(path: Path, *, error_code: str = "E_BAD_MANIFEST") -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_strict_object)
    except DuplicateKeyError as exc:
        raise GitOpsError(
            error_code,
            f"duplicate JSON key: {exc.args[0]}",
            {"path": str(path), "key": exc.args[0]},
        ) from exc
    except FileNotFoundError as exc:
        raise GitOpsError(
            "E_NOT_FOUND",
            f"file not found: {path}",
            {"path": str(path)},
        ) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GitOpsError(
            error_code,
            f"invalid JSON: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, (json_text(value)).encode("utf-8"))


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_int(
    value: Any,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    code: str = "E_BAD_MANIFEST",
    context: str | None = None,
) -> int:
    if not is_int(value):
        raise GitOpsError(
            code,
            f"{field} must be an integer",
            {"field": field, "value": value, "context": context},
        )
    if minimum is not None and value < minimum:
        raise GitOpsError(
            code,
            f"{field} must be >= {minimum}",
            {"field": field, "value": value, "context": context},
        )
    if maximum is not None and value > maximum:
        raise GitOpsError(
            code,
            f"{field} must be <= {maximum}",
            {"field": field, "value": value, "context": context},
        )
    return value


def require_app_name(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not APP_NAME_RE.fullmatch(value):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            "invalid canonical application name",
            {"context": context, "name": value},
        )
    return value


def require_version(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not SEMVER_RE.fullmatch(value):
        raise GitOpsError(
            "E_BAD_VERSION",
            "image version must be a semantic version such as 1.2.3",
            {"context": context, "version": value},
        )
    return value


def require_mapping(value: Any, field: str, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            f"{field} must be an object",
            {"context": context, "field": field},
        )
    return value


def require_list(value: Any, field: str, *, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitOpsError(
            "E_BAD_MANIFEST",
            f"{field} must be an array",
            {"context": context, "field": field},
        )
    return value


def semver_key(version: str) -> tuple[int, int, int, int, str]:
    """Return a comparison key; prereleases sort below the final release."""

    core, _, build = version.partition("+")
    release, separator, prerelease = core.partition("-")
    major, minor, patch = (int(part) for part in release.split("."))
    if separator:
        return (major, minor, patch, 0, prerelease)
    return (major, minor, patch, 1, "")


def find_cycle(nodes: list[str], dependencies: dict[str, list[str]]) -> list[str] | None:
    """Find a deterministic cycle in a dependency graph."""

    state: dict[str, int] = {}
    stack: list[str] = []
    positions: dict[str, int] = {}

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        positions[node] = len(stack)
        stack.append(node)
        for dependency in sorted(set(dependencies.get(node, []))):
            if dependency not in dependencies and dependency not in state:
                continue
            marker = state.get(dependency, 0)
            if marker == 1:
                return stack[positions[dependency] :] + [dependency]
            if marker == 0:
                cycle = visit(dependency)
                if cycle is not None:
                    return cycle
        stack.pop()
        positions.pop(node, None)
        state[node] = 2
        return None

    for node in sorted(nodes):
        if state.get(node, 0) == 0:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


def validate_dependencies(
    nodes: list[str],
    dependencies: dict[str, list[str]],
    *,
    context: str,
) -> None:
    node_set = set(nodes)
    for app in sorted(nodes):
        upstreams = dependencies.get(app, [])
        missing = sorted(set(upstreams) - node_set)
        if missing:
            raise GitOpsError(
                "E_DEP_MISSING",
                "manifest references an unknown upstream application",
                {"app": app, "context": context, "missing": missing},
            )
    cycle = find_cycle(nodes, dependencies)
    if cycle is not None:
        raise GitOpsError(
            "E_DEP_CYCLE",
            "dependency cycle detected",
            {"context": context, "cycle": cycle},
        )


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Advisory lock that works on POSIX and Windows without path assumptions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True

