from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import KGError, error_dict
from .util import (
    atomic_write_json,
    content_checksum,
    read_json,
    sha256_bytes,
)


SNAPSHOT_CATEGORIES = ("config", "dictionary", "relation_schema", "graph_stats")
MANIFEST_VERSION = 1


class SnapshotStore:
    """Versioned, whole-file snapshot store with checksums and atomic swaps."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.manifest_path = self.root / "manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return {
                "manifest_version": MANIFEST_VERSION,
                "entries": {},
            }
        try:
            manifest = read_json(self.manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照清单无法读取",
                details={
                    "snapshot_category": "manifest",
                    "version": None,
                    "error": str(exc),
                },
            ) from exc
        if not isinstance(manifest, dict):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照清单必须是 JSON 对象",
                details={"snapshot_category": "manifest", "version": None},
            )
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "不支持或缺失 manifest_version",
                details={
                    "snapshot_category": "manifest",
                    "version": manifest.get("manifest_version"),
                },
            )
        if not isinstance(manifest.get("entries"), dict):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照清单 entries 字段无效",
                details={"snapshot_category": "manifest", "version": None},
            )
        return manifest

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        atomic_write_json(self.manifest_path, manifest)

    def current_entry(self, category: str) -> dict[str, Any] | None:
        if category not in SNAPSHOT_CATEGORIES:
            raise KGError(
                "E_SNAPSHOT_CATEGORY",
                f"未知快照类别: {category}",
                details={"snapshot_category": category},
            )
        return self._load_manifest()["entries"].get(category)

    def next_version(self, category: str) -> int:
        entry = self.current_entry(category)
        if entry is None:
            return 1
        return int(entry["version"]) + 1

    def _snapshot_path(self, category: str, version: int) -> Path:
        return self.root / f"{category}-v{version:06d}.json"

    def load(self, category: str) -> dict[str, Any]:
        entry = self.current_entry(category)
        if entry is None:
            raise KGError(
                "E_SNAPSHOT_MISSING",
                f"快照类别 {category} 尚未建立",
                details={"snapshot_category": category, "version": None},
            )
        version = int(entry["version"])
        path = self.root / str(entry["file"])
        if not path.exists():
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                f"快照文件缺失: {path.name}",
                details={"snapshot_category": category, "version": version},
            )
        try:
            raw_bytes = path.read_bytes()
            wrapper = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                f"快照文件损坏: {path.name}",
                details={
                    "snapshot_category": category,
                    "version": version,
                    "error": str(exc),
                },
            ) from exc
        file_digest = sha256_bytes(raw_bytes)
        if file_digest != entry.get("file_sha256"):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                f"快照文件校验和不符: {path.name}",
                details={
                    "snapshot_category": category,
                    "version": version,
                    "expected_file_sha256": entry.get("file_sha256"),
                    "actual_file_sha256": file_digest,
                },
            )
        if not isinstance(wrapper, dict):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照外壳必须是 JSON 对象",
                details={"snapshot_category": category, "version": version},
            )
        if wrapper.get("category") != category or wrapper.get("version") != version:
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照外壳的类别或版本与清单不符",
                details={
                    "snapshot_category": category,
                    "version": version,
                    "wrapper_category": wrapper.get("category"),
                    "wrapper_version": wrapper.get("version"),
                },
            )
        payload = wrapper.get("payload")
        payload_checksum = content_checksum(payload)
        if payload_checksum != wrapper.get("checksum"):
            raise KGError(
                "E_SNAPSHOT_CORRUPT",
                "快照内容校验和不符",
                details={
                    "snapshot_category": category,
                    "version": version,
                    "expected_checksum": wrapper.get("checksum"),
                    "actual_checksum": payload_checksum,
                },
            )
        return payload

    def install(
        self,
        category: str,
        version: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if category not in SNAPSHOT_CATEGORIES:
            raise KGError(
                "E_SNAPSHOT_CATEGORY",
                f"未知快照类别: {category}",
                details={"snapshot_category": category},
            )
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise KGError(
                "E_SNAPSHOT_VERSION",
                "快照版本必须为正整数",
                details={"snapshot_category": category, "version": version},
            )
        manifest = self._load_manifest()
        existing = manifest["entries"].get(category)
        payload_checksum = content_checksum(payload)
        if existing is not None:
            current_version = int(existing["version"])
            if version < current_version:
                raise KGError(
                    "E_SNAPSHOT_VERSION",
                    f"快照 {category} 版本必须单调递增",
                    details={
                        "snapshot_category": category,
                        "version": version,
                        "current_version": current_version,
                    },
                )
            self.load(category)
            if version == current_version:
                if payload_checksum != existing["payload_checksum"]:
                    raise KGError(
                        "E_SNAPSHOT_CORRUPT",
                        f"快照 {category} 同版本内容发生变化，拒绝增量修补",
                        details={
                            "snapshot_category": category,
                            "version": version,
                            "expected_payload_checksum": existing["payload_checksum"],
                            "actual_payload_checksum": payload_checksum,
                        },
                    )
                return {
                    "category": category,
                    "mode": "unchanged",
                    "version": version,
                    "checksum": payload_checksum,
                    "replaced_version": None,
                    "atomic": True,
                }

        wrapper = {
            "category": category,
            "version": version,
            "checksum": payload_checksum,
            "payload": payload,
        }
        atomic_write_json(self._snapshot_path(category, version), wrapper)
        wrapped_bytes = self._snapshot_path(category, version).read_bytes()
        file_digest = sha256_bytes(wrapped_bytes)
        old_file = existing.get("file") if existing else None
        manifest["entries"][category] = {
            "version": version,
            "checksum": payload_checksum,
            "payload_checksum": payload_checksum,
            "file": self._snapshot_path(category, version).name,
            "file_sha256": file_digest,
        }
        self._write_manifest(manifest)
        if old_file and old_file != manifest["entries"][category]["file"]:
            old_path = self.root / str(old_file)
            if old_path.exists():
                old_path.unlink()
        return {
            "category": category,
            "mode": "whole_replace" if existing else "create",
            "version": version,
            "checksum": payload_checksum,
            "replaced_version": int(existing["version"]) if existing else None,
            "atomic": True,
        }

    def verify_all(self) -> dict[str, Any]:
        try:
            manifest = self._load_manifest()
        except KGError as exc:
            return {
                "valid": False,
                "manifest_version": None,
                "checks": [],
                "errors": [exc.to_dict()],
            }
        checks: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for category in SNAPSHOT_CATEGORIES:
            if category not in manifest["entries"]:
                errors.append(
                    error_dict(
                        "E_SNAPSHOT_MISSING",
                        f"快照类别 {category} 缺失",
                        category=category,
                        snapshot_category=category,
                        version=None,
                    )
                )
                checks.append(
                    {
                        "category": category,
                        "present": False,
                        "valid": None,
                    }
                )
                continue
            version = int(manifest["entries"][category]["version"])
            try:
                self.load(category)
            except KGError as exc:
                record = exc.to_dict()
                record["category"] = category
                record.setdefault("details", {})["snapshot_category"] = category
                record["details"].setdefault("version", version)
                errors.append(record)
                checks.append(
                    {
                        "category": category,
                        "present": True,
                        "version": version,
                        "valid": False,
                    }
                )
            else:
                checks.append(
                    {
                        "category": category,
                        "present": True,
                        "version": version,
                        "valid": True,
                        "checksum": manifest["entries"][category]["checksum"],
                    }
                )
        return {
            "valid": not errors,
            "manifest_version": manifest.get("manifest_version"),
            "checks": checks,
            "errors": errors,
        }

