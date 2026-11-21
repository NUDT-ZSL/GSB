"""Content-addressed node snapshots and immutable generation manifests."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import fail
from .util import (
    read_json,
    sha256_value,
    write_json_atomic,
)


def make_cache_key(node: str, node_version: str, key_details: dict[str, Any]) -> str:
    return sha256_value(
        {
            "node": node,
            "node_version": node_version,
            "key_details": key_details,
        }
    )


@dataclass(frozen=True)
class Artifact:
    node: str
    node_version: str
    cache_key: str
    content_hash: str
    upstream_hash: str | None
    key_details: dict[str, Any]
    payload: dict[str, Any]

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": 1,
            "node": self.node,
            "node_version": self.node_version,
            "cache_key": self.cache_key,
            "content_hash": self.content_hash,
            "upstream_hash": self.upstream_hash,
            "key_details": self.key_details,
            "payload": self.payload,
        }
        record["record_checksum"] = sha256_value(record)
        return record

    @classmethod
    def from_record(
        cls, record: dict[str, Any], *, entry: str, version: int
    ) -> "Artifact":
        try:
            checksum = str(record["record_checksum"])
            body = {key: value for key, value in record.items() if key != "record_checksum"}
            if checksum != sha256_value(body):
                fail(
                    "E_CACHE_CORRUPT",
                    "snapshot entry checksum mismatch",
                    entry=entry,
                    version=version,
                )
            node = str(record["node"])
            node_version = str(record["node_version"])
            cache_key = str(record["cache_key"])
            content_hash = str(record["content_hash"])
            upstream_hash = record.get("upstream_hash")
            key_details = record["key_details"]
            payload = record["payload"]
        except (KeyError, TypeError, ValueError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot entry is missing required fields",
                entry=entry,
                version=version,
                error=str(exc),
            )
        expected_key = make_cache_key(node, node_version, key_details)
        if cache_key != expected_key:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot cache key does not match its key details",
                entry=entry,
                version=version,
                expected_cache_key=expected_key,
                actual_cache_key=cache_key,
            )
        expected_content = sha256_value(payload)
        if content_hash != expected_content:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot payload content hash mismatch",
                entry=entry,
                version=version,
                expected_content_hash=expected_content,
                actual_content_hash=content_hash,
            )
        if upstream_hash is not None:
            declared_upstream = key_details.get("upstream_hash")
            if declared_upstream != upstream_hash:
                fail(
                    "E_CACHE_CORRUPT",
                    "snapshot upstream hash does not match key details",
                    entry=entry,
                    version=version,
                )
        return cls(
            node=node,
            node_version=node_version,
            cache_key=cache_key,
            content_hash=content_hash,
            upstream_hash=upstream_hash,
            key_details=key_details,
            payload=payload,
        )


def make_artifact(
    node: str,
    node_version: str,
    key_details: dict[str, Any],
    payload: dict[str, Any],
    upstream_hash: str | None,
) -> Artifact:
    return Artifact(
        node=node,
        node_version=node_version,
        cache_key=make_cache_key(node, node_version, key_details),
        content_hash=sha256_value(payload),
        upstream_hash=upstream_hash,
        key_details=key_details,
        payload=payload,
    )


class SnapshotStore:
    """Maintains immutable generations and a monotonic active pointer."""

    def __init__(self, root: str | Path, max_entries: int):
        self.root = Path(root)
        self.max_entries = max_entries
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_file = self.root / "CURRENT.json"

    def _generation_dir(self, version: int) -> Path:
        return self.root / f"v{version:06d}"

    def _manifest_path(self, version: int) -> Path:
        return self._generation_dir(version) / "manifest.json"

    def _load_manifest_file(self, version: int) -> dict[str, Any]:
        path = self._manifest_path(version)
        try:
            manifest = read_json(path)
        except FileNotFoundError:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot manifest is missing",
                entry="manifest",
                version=version,
            )
        except ValueError as exc:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot manifest is not valid JSON",
                entry="manifest",
                version=version,
                error=str(exc),
            )
        try:
            checksum = manifest["manifest_checksum"]
        except (KeyError, TypeError):
            fail(
                "E_CACHE_CORRUPT",
                "snapshot manifest has no checksum",
                entry="manifest",
                version=version,
            )
        body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
        if checksum != sha256_value(body):
            fail(
                "E_CACHE_CORRUPT",
                "snapshot manifest checksum mismatch",
                entry="manifest",
                version=version,
            )
        if int(manifest.get("version", -1)) != version:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot manifest version mismatch",
                entry="manifest",
                version=version,
            )
        return manifest

    def current_manifest(self) -> dict[str, Any] | None:
        if not self.current_file.exists():
            return None
        try:
            current = read_json(self.current_file)
        except (OSError, ValueError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "CURRENT pointer is invalid",
                entry="CURRENT",
                version=None,
                error=str(exc),
            )
        version = int(current.get("version", -1))
        if version <= 0:
            fail(
                "E_CACHE_CORRUPT",
                "CURRENT pointer has no valid version",
                entry="CURRENT",
                version=version,
            )
        manifest = self._load_manifest_file(version)
        if manifest["manifest_checksum"] != current.get("manifest_checksum"):
            fail(
                "E_CACHE_CORRUPT",
                "CURRENT pointer checksum does not match the manifest",
                entry="CURRENT",
                version=version,
            )
        return manifest

    def current_version(self) -> int | None:
        manifest = self.current_manifest()
        return int(manifest["version"]) if manifest else None

    def history_versions(self) -> list[int]:
        versions: list[int] = []
        for path in self.root.iterdir():
            if path.is_dir() and path.name.startswith("v") and path.name[1:].isdigit():
                versions.append(int(path.name[1:]))
        return sorted(versions)

    def _generation_signature(
        self, config_identity: dict[str, Any], artifacts: Iterable[Artifact]
    ) -> str:
        entries = [
            {
                "node": artifact.node,
                "node_version": artifact.node_version,
                "cache_key": artifact.cache_key,
                "content_hash": artifact.content_hash,
                "upstream_hash": artifact.upstream_hash,
            }
            for artifact in sorted(artifacts, key=lambda item: item.node)
        ]
        return sha256_value({"config": config_identity, "entries": entries})

    def commit_generation(
        self,
        config_identity: dict[str, Any],
        artifacts: Iterable[Artifact],
        requested_version: int | None = None,
    ) -> tuple[int, bool]:
        artifact_list = list(artifacts)
        if len(artifact_list) > self.max_entries:
            fail(
                "E_RESOURCE_LIMIT",
                "snapshot entry limit exceeded",
                resource="max_snapshot_entries",
                limit=self.max_entries,
                entry_count=len(artifact_list),
            )
        duplicate_keys = len({artifact.cache_key for artifact in artifact_list}) != len(
            artifact_list
        )
        if duplicate_keys:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot generation contains duplicate cache keys",
                entry="generation",
                version=requested_version,
            )
        signature = self._generation_signature(config_identity, artifact_list)
        current = self.current_manifest()
        current_version = int(current["version"]) if current is not None else 0
        if (
            current is not None
            and int(config_identity.get("config_version", 0))
            < int(current.get("config", {}).get("config_version", 0))
        ):
            fail(
                "E_SNAPSHOT_ROLLBACK",
                "configuration version cannot move backwards",
                entry="config",
                version=int(config_identity.get("config_version", 0)),
                current_version=int(current["config"].get("config_version", 0)),
            )
        if requested_version is not None and int(requested_version) <= current_version:
            fail(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot versions must increase monotonically",
                entry="generation",
                version=int(requested_version),
                current_version=current_version,
            )
        if current is not None and current.get("generation_signature") == signature:
            return int(current["version"]), False

        version = current_version + 1 if requested_version is None else int(requested_version)
        if version <= current_version:
            fail(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot versions must increase monotonically",
                entry="generation",
                version=version,
                current_version=current_version,
            )
        if self._generation_dir(version).exists():
            fail(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot generation already exists and is immutable",
                entry="generation",
                version=version,
            )
        staging = self.root / f".staging-{uuid.uuid4().hex}"
        (staging / "entries").mkdir(parents=True, exist_ok=True)
        manifest_entries: list[dict[str, Any]] = []
        try:
            for artifact in artifact_list:
                node_dir = staging / "entries" / artifact.node
                node_dir.mkdir(parents=True, exist_ok=True)
                relative_path = Path("entries") / artifact.node / f"{artifact.cache_key}.json"
                write_json_atomic(staging / relative_path, artifact.to_record())
                manifest_entries.append(
                    {
                        "node": artifact.node,
                        "node_version": artifact.node_version,
                        "cache_key": artifact.cache_key,
                        "content_hash": artifact.content_hash,
                        "upstream_hash": artifact.upstream_hash,
                        "path": relative_path.as_posix(),
                    }
                )
            manifest_entries.sort(key=lambda item: (item["node"], item["cache_key"]))
            manifest_body = {
                "schema_version": 1,
                "version": version,
                "config": config_identity,
                "config_checksum": sha256_value(config_identity),
                "generation_signature": signature,
                "entries": manifest_entries,
            }
            manifest = dict(manifest_body)
            manifest["manifest_checksum"] = sha256_value(manifest_body)
            write_json_atomic(staging / "manifest.json", manifest)
            staging.replace(self._generation_dir(version))
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        write_json_atomic(
            self.current_file,
            {
                "version": version,
                "manifest_checksum": manifest["manifest_checksum"],
            },
        )
        return version, True

    def find_manifest_entry(
        self, manifest: dict[str, Any] | None, cache_key: str
    ) -> dict[str, Any] | None:
        if manifest is None:
            return None
        for entry in manifest.get("entries", []):
            if entry.get("cache_key") == cache_key:
                return entry
        return None

    def load_entry(
        self, cache_key: str, version: int | None = None
    ) -> Artifact | None:
        manifest = (
            self.current_manifest()
            if version is None
            else self._load_manifest_file(int(version))
        )
        if manifest is None:
            return None
        entry = self.find_manifest_entry(manifest, cache_key)
        if entry is None:
            return None
        return self._load_entry_from_manifest(manifest, entry)

    def load_any(self, cache_key: str) -> Artifact | None:
        current = self.current_manifest()
        if current is not None:
            entry = self.find_manifest_entry(current, cache_key)
            if entry is not None:
                return self._load_entry_from_manifest(current, entry)
        for version in reversed(self.history_versions()):
            if current is not None and version == int(current["version"]):
                continue
            manifest = self._load_manifest_file(version)
            entry = self.find_manifest_entry(manifest, cache_key)
            if entry is not None:
                return self._load_entry_from_manifest(manifest, entry)
        return None

    def _load_entry_from_manifest(
        self, manifest: dict[str, Any], entry: dict[str, Any]
    ) -> Artifact:
        version = int(manifest["version"])
        cache_key = str(entry["cache_key"])
        path = self._generation_dir(version) / str(entry["path"])
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot entry file is missing",
                entry=cache_key,
                version=version,
            )
        except OSError as exc:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot entry file cannot be read",
                entry=cache_key,
                version=version,
                error=str(exc),
            )
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "snapshot entry is not valid JSON",
                entry=cache_key,
                version=version,
                error=str(exc),
            )
        if not isinstance(record, dict):
            fail(
                "E_CACHE_CORRUPT",
                "snapshot entry root is not an object",
                entry=cache_key,
                version=version,
            )
        artifact = Artifact.from_record(record, entry=cache_key, version=version)
        if artifact.cache_key != cache_key:
            fail(
                "E_CACHE_CORRUPT",
                "manifest and entry cache keys differ",
                entry=cache_key,
                version=version,
            )
        if artifact.content_hash != entry.get("content_hash"):
            fail(
                "E_CACHE_CORRUPT",
                "manifest and entry content hashes differ",
                entry=cache_key,
                version=version,
            )
        return artifact

    def entry_count(self, version: int | None = None) -> int:
        manifest = (
            self.current_manifest()
            if version is None
            else self._load_manifest_file(int(version))
        )
        return len(manifest.get("entries", [])) if manifest else 0

    def entry_exists(self, version: int, cache_key: str) -> bool:
        try:
            manifest = self._load_manifest_file(version)
        except Exception:
            return False
        return self.find_manifest_entry(manifest, cache_key) is not None

    def entry_file_exists(self, version: int, cache_key: str) -> bool:
        try:
            manifest = self._load_manifest_file(version)
        except Exception:
            return False
        entry = self.find_manifest_entry(manifest, cache_key)
        if entry is None:
            return False
        return (self._generation_dir(version) / str(entry["path"])).is_file()
