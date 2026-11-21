"""Monotonic, checksummed snapshot replacement with rollback rejection."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, QUError
from .util import payload_checksum, read_json, sha256_obj, write_json


def _component_checksum(payload: dict[str, Any], component: str) -> str:
    value = payload.get(component)
    if not isinstance(value, dict):
        raise QUError(
            E_SNAPSHOT_CORRUPT,
            f"snapshot bundle is missing component {component}",
            stage="snapshot",
            category=f"missing_{component}",
        )
    expected = value.get("checksum")
    actual = payload_checksum(value)
    if expected != actual:
        raise QUError(
            E_SNAPSHOT_CORRUPT,
            f"{component} checksum mismatch inside snapshot",
            stage="snapshot",
            category=f"{component}_checksum",
            details={"expected": expected, "actual": actual},
        )
    return actual


class SnapshotManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_version: int | None = None
        self.current_payload: dict[str, Any] | None = None
        self.current_checksum: str | None = None

    def path_for(self, version: int) -> Path:
        return self.root / f"snapshot-v{version:04d}.json"

    def _validate_wrapper(self, wrapper: dict[str, Any], expected_version: int | None = None) -> dict[str, Any]:
        if not isinstance(wrapper, dict):
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot wrapper must be an object",
                stage="snapshot",
            )
        for field in ("version", "content_checksum", "payload"):
            if field not in wrapper:
                raise QUError(
                    E_SNAPSHOT_CORRUPT,
                    f"snapshot wrapper is missing {field}",
                    stage="snapshot",
                    category="missing_wrapper_field",
                    details={"field": field},
                )
        version = int(wrapper["version"])
        if expected_version is not None and version != expected_version:
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot filename version and content version differ",
                stage="snapshot",
                category="version_mismatch",
                details={"expected": expected_version, "actual": version},
            )
        payload = wrapper["payload"]
        if not isinstance(payload, dict):
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot payload must be an object",
                stage="snapshot",
            )
        actual = sha256_obj(payload)
        if wrapper["content_checksum"] != actual:
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot content checksum mismatch",
                stage="snapshot",
                category="snapshot_checksum",
                details={
                    "expected": wrapper["content_checksum"],
                    "actual": actual,
                },
            )
        taxonomy_checksum = _component_checksum(payload, "taxonomy")
        dictionary_checksum = _component_checksum(payload, "dictionary")
        config_checksum = _component_checksum(payload, "config")
        mappings_checksum = (
            _component_checksum(payload, "mappings")
            if "mappings" in payload
            else None
        )
        component_versions = [
            int(payload["taxonomy"].get("version", 0)),
            int(payload["dictionary"].get("version", 0)),
            int(payload["config"].get("version", 0)),
        ]
        if "mappings" in payload:
            component_versions.append(int(payload["mappings"].get("version", 0)))
        if any(version < component_version for component_version in component_versions):
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version predates a component version",
                stage="snapshot",
                category="component_version_ahead",
                details={
                    "snapshot_version": version,
                    "component_versions": component_versions,
                },
            )
        return {
            "version": version,
            "content_checksum": actual,
            "payload": payload,
            "taxonomy_checksum": taxonomy_checksum,
            "dictionary_checksum": dictionary_checksum,
            "config_checksum": config_checksum,
            "mappings_checksum": mappings_checksum,
        }

    def save(self, version: int, payload: dict[str, Any]) -> Path:
        if self.current_version is not None and version < self.current_version:
            raise QUError(
                E_SNAPSHOT_ROLLBACK,
                "cannot save a snapshot older than the active version",
                stage="snapshot",
                category="version_regression",
                details={
                    "active_version": self.current_version,
                    "attempted_version": version,
                },
            )
        _component_checksum(payload, "taxonomy")
        _component_checksum(payload, "dictionary")
        _component_checksum(payload, "config")
        if "mappings" in payload:
            _component_checksum(payload, "mappings")
        content_checksum = sha256_obj(payload)
        path = self.path_for(version)
        if path.exists():
            existing = self._validate_wrapper(read_json(path), version)
            if existing["content_checksum"] == content_checksum:
                return path
            if version == self.current_version:
                raise QUError(
                    E_SNAPSHOT_CORRUPT,
                    "same version cannot be overwritten with different content",
                    stage="snapshot",
                    category="same_version_content_conflict",
                    details={"version": version},
                )
            raise QUError(
                E_SNAPSHOT_ROLLBACK,
                "older snapshot content cannot overwrite a newer snapshot",
                stage="snapshot",
                category="content_regression",
                details={"version": version},
            )
        wrapper = {
            "version": version,
            "content_checksum": content_checksum,
            "payload": payload,
        }
        write_json(path, wrapper)
        return path

    def load_file(self, path: str | Path, expected_version: int | None = None) -> dict[str, Any]:
        try:
            wrapper = read_json(path)
        except Exception as exc:
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot cannot be parsed",
                stage="snapshot",
                category="json_parse",
                details={"path": str(path), "error": str(exc)},
            ) from exc
        return self._validate_wrapper(wrapper, expected_version)

    def activate(self, version: int) -> dict[str, Any]:
        if self.current_version is not None and version < self.current_version:
            raise QUError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot rollback is forbidden",
                stage="snapshot",
                category="version_regression",
                details={
                    "active_version": self.current_version,
                    "attempted_version": version,
                },
            )
        path = self.path_for(version)
        if not path.exists():
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "snapshot file is missing",
                stage="snapshot",
                category="missing_snapshot",
                details={"version": version, "path": str(path)},
            )
        loaded = self.load_file(path, version)
        self.current_version = version
        self.current_payload = loaded["payload"]
        self.current_checksum = loaded["content_checksum"]
        write_json(
            self.root / "active.json",
            {
                "active_version": version,
                "content_checksum": loaded["content_checksum"],
                "snapshot_file": path.name,
            },
        )
        return loaded

    def active_bundle(self) -> dict[str, Any]:
        if self.current_payload is None:
            raise QUError(
                E_SNAPSHOT_CORRUPT,
                "no snapshot is active",
                stage="snapshot",
                category="no_active_snapshot",
            )
        return self.current_payload


def snapshot_wrapper_bytes(version: int, payload: dict[str, Any]) -> bytes:
    from .util import pretty_json

    wrapper = {
        "version": version,
        "content_checksum": sha256_obj(payload),
        "payload": payload,
    }
    return pretty_json(wrapper).encode("utf-8")


def write_corrupt_snapshot(path: str | Path, version: int, payload: dict[str, Any]) -> None:
    raw = snapshot_wrapper_bytes(version, payload)
    expected = sha256_obj(payload).encode("ascii")
    raw = raw.replace(expected, b"0" * 64, 1)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, target)

