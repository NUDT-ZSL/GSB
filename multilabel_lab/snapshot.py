"""Versioned, checksummed, atomically replaced experiment snapshots."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .errors import E_SNAPSHOT_VERSION, LabError, SnapshotCorrupt
from .hashing import canonical_json_bytes, sha256_json


CATEGORIES = ("config", "projection", "annotations")
SNAPSHOT_SCHEMA = "multilabel-lab.snapshot.v1"


class SnapshotStore:
    """Store all three categories in one atomically replaced bundle.

    The bundle is either completely valid or rejected. A load never exposes a
    partially parsed mix of old and new categories.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "snapshot.json"
        self.temp_path = self.directory / "snapshot.json.tmp"
        self._active: dict[str, Any] | None = None

    def _make_envelope(self, category: str, version: int, payload: Any) -> dict[str, Any]:
        content = {"category": category, "version": version, "payload": payload}
        return {
            "category": category,
            "version": version,
            "checksum": sha256_json(content),
            "payload": copy.deepcopy(payload),
        }

    def commit(
        self,
        payloads: dict[str, Any],
        *,
        versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Replace the complete snapshot with a new validated bundle."""

        missing = [category for category in CATEGORIES if category not in payloads]
        if missing:
            raise LabError(
                E_SNAPSHOT_VERSION,
                "a snapshot commit must contain every category",
                stage="snapshot",
                details={"missing": missing},
            )

        previous = self._read_bundle(allow_missing=True)
        previous_versions: dict[str, int] = {}
        if previous is not None:
            for category in CATEGORIES:
                previous_versions[category] = previous["categories"][category]["version"]

        resolved_versions: dict[str, int] = {}
        for category in CATEGORIES:
            requested = (versions or {}).get(category, previous_versions.get(category, 0) + 1)
            if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
                raise LabError(
                    E_SNAPSHOT_VERSION,
                    "snapshot versions must be positive integers",
                    stage="snapshot",
                    details={"category": category, "version": requested},
                )
            if category in previous_versions and requested <= previous_versions[category]:
                raise LabError(
                    E_SNAPSHOT_VERSION,
                    "snapshot versions must increase monotonically",
                    stage="snapshot",
                    details={
                        "category": category,
                        "previous_version": previous_versions[category],
                        "requested_version": requested,
                    },
                )
            resolved_versions[category] = requested

        bundle = {
            "schema": SNAPSHOT_SCHEMA,
            "categories": {
                category: self._make_envelope(
                    category, resolved_versions[category], payloads[category]
                )
                for category in CATEGORIES
            },
        }
        self._validate_bundle(bundle)

        self.directory.mkdir(parents=True, exist_ok=True)
        data = canonical_json_bytes(bundle) + b"\n"
        try:
            with self.temp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(self.temp_path, self.path)
            try:
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Directory fsync is not available on every supported platform.
                pass
        finally:
            if self.temp_path.exists():
                self.temp_path.unlink()

        # Let load() compare the newly written bundle with the bundle that was
        # active before this replacement so replacement metadata is truthful.
        self._active = copy.deepcopy(previous) if previous is not None else None
        return self.load()

    def _read_bundle(self, *, allow_missing: bool = False) -> dict[str, Any] | None:
        if not self.path.exists():
            if allow_missing:
                return None
            raise SnapshotCorrupt(
                "snapshot bundle is missing",
                category="__bundle__",
                version=None,
            )
        try:
            raw = self.path.read_bytes()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SnapshotCorrupt(
                "snapshot bundle cannot be parsed",
                category="__bundle__",
                version=None,
                details={"error": type(exc).__name__},
            ) from exc
        if not isinstance(data, dict):
            raise SnapshotCorrupt(
                "snapshot bundle must be a JSON object",
                category="__bundle__",
                version=None,
            )
        try:
            self._validate_bundle(data)
        except SnapshotCorrupt:
            raise
        except LabError as exc:
            details = exc.details or {}
            raise SnapshotCorrupt(
                exc.message,
                category=str(details.get("category", "__bundle__")),
                version=details.get("version"),
                details=details,
            ) from exc
        data["bundle_checksum"] = sha256_json(
            {"schema": data["schema"], "categories": data["categories"]}
        )
        return data

    def _validate_bundle(self, bundle: dict[str, Any]) -> None:
        if bundle.get("schema") != SNAPSHOT_SCHEMA:
            raise SnapshotCorrupt(
                "snapshot schema is invalid",
                category="__bundle__",
                version=None,
                details={"actual": bundle.get("schema")},
            )
        categories = bundle.get("categories")
        if not isinstance(categories, dict):
            raise SnapshotCorrupt(
                "snapshot categories are missing",
                category="__bundle__",
                version=None,
            )
        if set(categories) != set(CATEGORIES):
            raise SnapshotCorrupt(
                "snapshot categories are incomplete or unknown",
                category="__bundle__",
                version=None,
                details={"actual": sorted(categories)},
            )

        for category in CATEGORIES:
            envelope = categories.get(category)
            version = envelope.get("version") if isinstance(envelope, dict) else None
            if not isinstance(envelope, dict):
                raise SnapshotCorrupt(
                    "snapshot category envelope is invalid",
                    category=category,
                    version=version,
                )
            if envelope.get("category") != category:
                raise SnapshotCorrupt(
                    "snapshot category name is invalid",
                    category=category,
                    version=version,
                )
            if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
                raise SnapshotCorrupt(
                    "snapshot category version is invalid",
                    category=category,
                    version=version,
                )
            content = {
                "category": category,
                "version": version,
                "payload": envelope.get("payload"),
            }
            expected = sha256_json(content)
            if envelope.get("checksum") != expected:
                raise SnapshotCorrupt(
                    "snapshot category checksum mismatch",
                    category=category,
                    version=version,
                    details={"expected": expected, "actual": envelope.get("checksum")},
                )

    def load(self) -> dict[str, Any]:
        bundle = self._read_bundle()
        assert bundle is not None
        previous = self._active
        changed: list[str] = []
        reasons: dict[str, str] = {}
        if previous is None:
            changed = list(CATEGORIES)
            reasons = {category: "loaded" for category in CATEGORIES}
        else:
            for category in CATEGORIES:
                old = previous["categories"][category]
                new = bundle["categories"][category]
                if old["version"] != new["version"]:
                    changed.append(category)
                    reasons[category] = "version"
                elif old["checksum"] != new["checksum"]:
                    changed.append(category)
                    reasons[category] = "checksum"
        self._active = copy.deepcopy(bundle)
        return {
            "snapshot": bundle,
            "replaced": bool(changed),
            "changed_categories": changed,
            "change_reasons": reasons,
        }

    def load_or_commit(
        self,
        payloads: dict[str, Any],
        *,
        versions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        if self.path.exists():
            return self.load()
        return self.commit(payloads, versions=versions)
