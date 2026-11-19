"""Immutable, checksummed, whole-snapshot persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import fail
from .util import clone, digest_value, flatten_weights

_COMPONENT_KEYS = {
    "conversations": "conversations",
    "results": "results",
    "risk": "risk",
    "weights": "weights",
    "config": "config",
}


def _component_payload(snapshot: dict[str, Any], category: str) -> Any:
    if category == "conversations":
        return snapshot["conversations"]
    if category == "results":
        return snapshot["results"]
    if category == "risk":
        return snapshot["risk_words"]
    if category == "weights":
        return flatten_weights(snapshot["conversations"])
    if category == "config":
        return snapshot["config"]
    raise KeyError(category)


def compute_checksums(payload: dict[str, Any]) -> dict[str, str]:
    return {
        category: digest_value(_component_payload(payload, category))
        for category in _COMPONENT_KEYS
    }


def _corrupt(category: str, version: Any, reason: str) -> None:
    fail(
        "E_SNAPSHOT_CORRUPT",
        "snapshot content checksum validation failed",
        category=category,
        version=version,
        reason=reason,
    )


@dataclass(frozen=True)
class Snapshot:
    snapshot_version: int
    versions: dict[str, int]
    conversations: dict[str, dict[str, Any]]
    config: dict[str, Any]
    risk_words: dict[str, Any]
    results: dict[str, dict[str, Any]]
    batch_count: int
    checksums: dict[str, str]
    snapshot_checksum: str

    @property
    def results_version(self) -> int:
        return int(self.versions["results"])

    @property
    def risk_version(self) -> int:
        return int(self.versions["risk"])

    @property
    def weights_version(self) -> int:
        return int(self.versions["weights"])

    @property
    def config_version(self) -> int:
        return int(self.versions["config"])

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "snapshot_version": self.snapshot_version,
            "versions": clone(self.versions),
            "checksums": clone(self.checksums),
            "snapshot_checksum": self.snapshot_checksum,
            "batch_count": self.batch_count,
            "conversations": clone(self.conversations),
            "config": clone(self.config),
            "risk_words": clone(self.risk_words),
            "results": clone(self.results),
        }

    def info(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "versions": clone(self.versions),
            "checksums": clone(self.checksums),
            "batch_count": self.batch_count,
            "conversation_count": len(self.conversations),
            "message_count": sum(
                len(conversation["messages"])
                for conversation in self.conversations.values()
            ),
        }


def build_snapshot(
    *,
    snapshot_version: int,
    versions: dict[str, int],
    conversations: dict[str, dict[str, Any]],
    config: dict[str, Any],
    risk_words: dict[str, Any],
    results: dict[str, dict[str, Any]],
    batch_count: int,
) -> Snapshot:
    payload = {
        "schema_version": 1,
        "snapshot_version": int(snapshot_version),
        "versions": {
            "results": int(versions["results"]),
            "risk": int(versions["risk"]),
            "weights": int(versions["weights"]),
            "config": int(versions["config"]),
        },
        "batch_count": int(batch_count),
        "conversations": clone(conversations),
        "config": clone(config),
        "risk_words": clone(risk_words),
        "results": clone(results),
    }
    checksums = compute_checksums(payload)
    payload["checksums"] = checksums
    snapshot_checksum = digest_value(payload)
    payload["snapshot_checksum"] = snapshot_checksum
    return Snapshot(
        snapshot_version=payload["snapshot_version"],
        versions=payload["versions"],
        conversations=payload["conversations"],
        config=payload["config"],
        risk_words=payload["risk_words"],
        results=payload["results"],
        batch_count=payload["batch_count"],
        checksums=checksums,
        snapshot_checksum=snapshot_checksum,
    )


def _validated_snapshot(payload: Any) -> Snapshot:
    if not isinstance(payload, dict):
        _corrupt("snapshot", None, "root is not an object")
    if payload.get("schema_version") != 1:
        _corrupt("snapshot", payload.get("snapshot_version"), "unsupported schema")
    snapshot_version = payload.get("snapshot_version")
    if (
        isinstance(snapshot_version, bool)
        or not isinstance(snapshot_version, int)
        or snapshot_version < 1
    ):
        _corrupt("snapshot", snapshot_version, "invalid snapshot_version")
    versions = payload.get("versions")
    if not isinstance(versions, dict):
        _corrupt("snapshot", snapshot_version, "missing versions")
    for name in ("results", "risk", "weights", "config"):
        version = versions.get(name)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            _corrupt(name, version, "invalid component version")
    for required in (
        "conversations",
        "config",
        "risk_words",
        "results",
        "batch_count",
        "checksums",
        "snapshot_checksum",
    ):
        if required not in payload:
            _corrupt("snapshot", snapshot_version, f"missing {required}")

    expected = compute_checksums(payload)
    declared = payload.get("checksums")
    if not isinstance(declared, dict):
        _corrupt("snapshot", snapshot_version, "checksums is not an object")
    for category in _COMPONENT_KEYS:
        if declared.get(category) != expected[category]:
            component_version = (
                snapshot_version if category == "conversations" else versions.get(category)
            )
            _corrupt(category, component_version, "component checksum mismatch")

    checksum_without_overall = {
        key: value for key, value in payload.items() if key != "snapshot_checksum"
    }
    if payload.get("snapshot_checksum") != digest_value(checksum_without_overall):
        _corrupt("snapshot", snapshot_version, "overall checksum mismatch")

    return Snapshot(
        snapshot_version=snapshot_version,
        versions={
            "results": int(versions["results"]),
            "risk": int(versions["risk"]),
            "weights": int(versions["weights"]),
            "config": int(versions["config"]),
        },
        conversations=clone(payload["conversations"]),
        config=clone(payload["config"]),
        risk_words=clone(payload["risk_words"]),
        results=clone(payload["results"]),
        batch_count=int(payload["batch_count"]),
        checksums=clone(declared),
        snapshot_checksum=payload["snapshot_checksum"],
    )


def validate_snapshot(snapshot: Snapshot) -> Snapshot:
    """Revalidate a live snapshot through the same persistence checks."""

    return _validated_snapshot(snapshot.to_payload())


class SnapshotStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(
        self,
        minimum_versions: dict[str, int] | None = None,
    ) -> Snapshot:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            fail("E_INPUT", "snapshot file does not exist", path=str(self.path))
        except (OSError, json.JSONDecodeError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "snapshot cannot be parsed",
                category="snapshot",
                version=None,
                reason=str(exc),
            )
        snapshot = _validated_snapshot(payload)
        if minimum_versions is not None:
            for category in ("results", "risk", "weights", "config"):
                current = int(minimum_versions.get(category, 1))
                loaded = snapshot.versions[category]
                if loaded < current:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot component version would move backwards",
                        category=category,
                        version=loaded,
                        current_version=current,
                    )
            if snapshot.snapshot_version < int(minimum_versions.get("snapshot", 1)):
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot version would move backwards",
                    category="snapshot",
                    version=snapshot.snapshot_version,
                    current_version=int(minimum_versions["snapshot"]),
                )
        return snapshot

    def save(self, snapshot: Snapshot) -> None:
        payload = snapshot.to_payload()
        validated = _validated_snapshot(payload)
        if validated.snapshot_version != snapshot.snapshot_version:
            _corrupt("snapshot", snapshot.snapshot_version, "snapshot round-trip mismatch")

        if self.path.exists():
            existing = self.load()
            if snapshot.snapshot_version <= existing.snapshot_version:
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot version must increase on replacement",
                    category="snapshot",
                    version=snapshot.snapshot_version,
                    current_version=existing.snapshot_version,
                )
            for category in ("results", "risk", "weights", "config"):
                if snapshot.versions[category] < existing.versions[category]:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "component version cannot move backwards",
                        category=category,
                        version=snapshot.versions[category],
                        current_version=existing.versions[category],
                    )

        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            fail(
                "E_IO",
                "could not atomically write snapshot",
                path=str(self.path),
                reason=str(exc),
            )

