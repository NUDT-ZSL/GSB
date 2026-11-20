"""Versioned whole-snapshot replacement with checksum validation."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    business_error,
)
from .util import canonical_json_bytes, sha256_json


SNAPSHOT_KINDS = ("config", "quant_params", "spectrum_stats")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")


def _checksum(kind: str, version: int, content: dict[str, Any]) -> str:
    return sha256_json(
        {
            "kind": kind,
            "version": version,
            "content": content,
        }
    )


@dataclass(frozen=True)
class Snapshot:
    kind: str
    version: int
    checksum: str
    content: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "content": copy.deepcopy(self.content),
        }

    def content_digest(self) -> str:
        return sha256_json(self.content)


@dataclass(frozen=True)
class SnapshotLoadOutcome:
    kind: str
    version: int
    action: str
    previous_version: int | None
    checksum: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "action": self.action,
            "previous_version": self.previous_version,
            "checksum": self.checksum,
        }


def create_snapshot(
    kind: str,
    version: int,
    content: dict[str, Any],
) -> Snapshot:
    if kind not in SNAPSHOT_KINDS:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_create",
            f"snapshot:{kind}",
            f"unknown snapshot kind: {kind!r}",
            known_kinds=list(SNAPSHOT_KINDS),
        )
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_create",
            f"snapshot:{kind}",
            "snapshot version must be a positive integer",
            version=version,
        )
    if not isinstance(content, dict):
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_create",
            f"snapshot:{kind}",
            "snapshot content must be an object",
        )
    try:
        canonical_json_bytes(content)
    except (TypeError, ValueError) as exc:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_create",
            f"snapshot:{kind}",
            "snapshot content is not canonical-JSON serializable",
            error=type(exc).__name__,
        ) from exc
    return Snapshot(
        kind=kind,
        version=version,
        checksum=_checksum(kind, version, content),
        content=copy.deepcopy(content),
    )


def snapshot_from_mapping(value: dict[str, Any]) -> Snapshot:
    required = ("kind", "version", "checksum", "content")
    if not isinstance(value, dict) or any(key not in value for key in required):
        kind = value.get("kind") if isinstance(value, dict) else "unknown"
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot requires kind, version, checksum, and content",
        )
    kind = value["kind"]
    version = value["version"]
    checksum = value["checksum"]
    content = value["content"]
    if kind not in SNAPSHOT_KINDS:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            f"unknown snapshot kind: {kind!r}",
            known_kinds=list(SNAPSHOT_KINDS),
        )
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot version must be a positive integer",
            version=version,
        )
    if not isinstance(content, dict):
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot content must be an object",
        )
    if not isinstance(checksum, str) or not _CHECKSUM_RE.fullmatch(checksum):
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot checksum format is invalid",
            checksum=checksum,
        )
    try:
        computed = _checksum(kind, version, content)
    except (TypeError, ValueError) as exc:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot content is not canonical-JSON serializable",
            error=type(exc).__name__,
        ) from exc
    if computed != checksum:
        raise business_error(
            E_SNAPSHOT_CORRUPT,
            "snapshot_load",
            f"snapshot:{kind}",
            "snapshot checksum mismatch",
            declared_checksum=checksum,
            computed_checksum=computed,
            version=version,
        )
    return Snapshot(
        kind=kind,
        version=version,
        checksum=checksum,
        content=copy.deepcopy(content),
    )


class SnapshotStore:
    """In-memory store whose load operation always replaces whole content."""

    def __init__(self) -> None:
        self._current: dict[str, Snapshot] = {}
        self._outcomes: list[SnapshotLoadOutcome] = []

    def load(self, value: Snapshot | dict[str, Any]) -> SnapshotLoadOutcome:
        snapshot = (
            value
            if isinstance(value, Snapshot)
            else snapshot_from_mapping(value)
        )
        if not isinstance(snapshot.content, dict):
            raise business_error(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                f"snapshot:{snapshot.kind}",
                "snapshot content must be an object",
            )
        try:
            computed = _checksum(
                snapshot.kind,
                snapshot.version,
                snapshot.content,
            )
        except (TypeError, ValueError) as exc:
            raise business_error(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                f"snapshot:{snapshot.kind}",
                "snapshot content is not canonical-JSON serializable",
                error=type(exc).__name__,
            ) from exc
        if computed != snapshot.checksum:
            raise business_error(
                E_SNAPSHOT_CORRUPT,
                "snapshot_load",
                f"snapshot:{snapshot.kind}",
                "snapshot checksum mismatch",
                declared_checksum=snapshot.checksum,
                computed_checksum=computed,
                version=snapshot.version,
            )
        current = self._current.get(snapshot.kind)
        if current is None:
            self._current[snapshot.kind] = Snapshot(
                kind=snapshot.kind,
                version=snapshot.version,
                checksum=snapshot.checksum,
                content=copy.deepcopy(snapshot.content),
            )
            outcome = SnapshotLoadOutcome(
                kind=snapshot.kind,
                version=snapshot.version,
                action="loaded",
                previous_version=None,
                checksum=snapshot.checksum,
            )
            self._outcomes.append(outcome)
            return outcome
        if snapshot.version < current.version:
            raise business_error(
                E_SNAPSHOT_ROLLBACK,
                "snapshot_load",
                f"snapshot:{snapshot.kind}",
                (
                    f"refusing to replace version {current.version} "
                    f"with older version {snapshot.version}"
                ),
                current_version=current.version,
                attempted_version=snapshot.version,
                attempted_checksum=snapshot.checksum,
            )
        if snapshot.version == current.version:
            if snapshot.checksum != current.checksum:
                raise business_error(
                    E_SNAPSHOT_CORRUPT,
                    "snapshot_load",
                    f"snapshot:{snapshot.kind}",
                    "same version has a different checksum",
                    current_version=current.version,
                    current_checksum=current.checksum,
                    attempted_checksum=snapshot.checksum,
                )
            outcome = SnapshotLoadOutcome(
                kind=snapshot.kind,
                version=snapshot.version,
                action="unchanged",
                previous_version=current.version,
                checksum=snapshot.checksum,
            )
            self._outcomes.append(outcome)
            return outcome
        self._current[snapshot.kind] = Snapshot(
            kind=snapshot.kind,
            version=snapshot.version,
            checksum=snapshot.checksum,
            content=copy.deepcopy(snapshot.content),
        )
        outcome = SnapshotLoadOutcome(
            kind=snapshot.kind,
            version=snapshot.version,
            action="replaced",
            previous_version=current.version,
            checksum=snapshot.checksum,
        )
        self._outcomes.append(outcome)
        return outcome

    def current(self, kind: str) -> Snapshot | None:
        snapshot = self._current.get(kind)
        return copy.deepcopy(snapshot) if snapshot is not None else None

    def state(self) -> dict[str, Any]:
        return {
            "current": {
                kind: snapshot.to_dict()
                for kind, snapshot in sorted(self._current.items())
            },
            "outcomes": [
                outcome.to_dict() for outcome in self._outcomes
            ],
        }
