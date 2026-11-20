"""Versioned, checksummed config snapshot storage with atomic replacement."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import parse_config
from .errors import (
    E_CACHE_CORRUPT,
    E_SNAPSHOT_CONFLICT,
    E_VERSION_STALE,
    RopeError,
)
from .locking import FileLock
from .models import ParsedConfig
from .util import (
    atomic_write_bytes,
    canonical_json_bytes,
    load_json_checked,
    sha256_json,
)


@dataclass(frozen=True)
class ConfigSnapshot:
    version: int
    config: ParsedConfig
    source_name: str
    checksum: str

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "config": self.config.to_dict(),
            "schema": "rope-config-snapshot/v1",
            "source_name": self.source_name,
            "version": self.version,
        }
        if include_checksum:
            body["checksum"] = self.checksum
        return body

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict(), pretty=True) + b"\n"

    @classmethod
    def create(
        cls,
        config: ParsedConfig,
        *,
        version: int,
        source_name: str,
    ) -> "ConfigSnapshot":
        if version <= 0:
            raise ValueError("snapshot version must be positive")
        body = {
            "config": config.to_dict(),
            "schema": "rope-config-snapshot/v1",
            "source_name": source_name,
            "version": version,
        }
        return cls(
            version=version,
            config=config,
            source_name=source_name,
            checksum=sha256_json(body),
        )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ConfigSnapshot":
        if document.get("schema") != "rope-config-snapshot/v1":
            raise RopeError(
                E_CACHE_CORRUPT,
                "snapshot schema is not supported",
                details={"schema": document.get("schema")},
            )
        checksum = document.get("checksum")
        body = {key: value for key, value in document.items() if key != "checksum"}
        if not isinstance(checksum, str) or checksum != sha256_json(body):
            raise RopeError(
                E_CACHE_CORRUPT,
                "snapshot checksum mismatch",
                details={"version": document.get("version")},
            )
        try:
            version = int(document["version"])
            source_name = str(document["source_name"])
            config = _parsed_from_document(document["config"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RopeError(
                E_CACHE_CORRUPT,
                "snapshot fields are corrupt",
                details={"reason": str(exc)},
            ) from exc
        snapshot = cls.create(config, version=version, source_name=source_name)
        if snapshot.checksum != checksum:
            raise RopeError(
                E_CACHE_CORRUPT,
                "snapshot checksum does not match its canonical content",
            )
        return snapshot


def _parsed_from_document(value: Any) -> ParsedConfig:
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    from .models import FrequencyAdjustment

    frequency_value = value.get("frequency_adjustment")
    frequency = (
        FrequencyAdjustment(
            method=str(frequency_value["method"]),
            factor=float(frequency_value["factor"]),
            block_tokens=(
                int(frequency_value["block_tokens"])
                if frequency_value.get("block_tokens") is not None
                else None
            ),
        )
        if frequency_value is not None
        else None
    )
    scales = value.get("per_head_scale")
    return ParsedConfig(
        source_version=str(value["source_version"]),
        source_line=int(value["source_line"]),
        source_span=str(value["source_span"]),
        source_text_sha256=str(value["source_text_sha256"]),
        base_theta=float(value["base_theta"]),
        pos_scale=float(value["pos_scale"]),
        block_size=int(value["block_size"]),
        variant=str(value["variant"]) if value.get("variant") is not None else None,
        frequency_adjustment=frequency,
        per_head_scale=tuple(float(item) for item in scales)
        if scales is not None
        else None,
        explicit_fields=tuple(str(item) for item in value["explicit_fields"]),
        field_locations={
            str(key): str(item)
            for key, item in dict(value.get("field_locations", {})).items()
        },
    )


class SnapshotStore:
    """One active snapshot at a time, committed by atomic manifest replacement."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.directory = self.root / "snapshots"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.directory / "manifest.json"
        self.lock_path = self.directory / ".snapshot.lock"

    def _read_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        return load_json_checked(self.manifest_path)

    def _snapshot_path(self, version: int) -> Path:
        return self.directory / f"snapshot-{version:020d}.json"

    def active(self) -> ConfigSnapshot | None:
        with FileLock(self.lock_path):
            manifest = self._read_manifest()
            if manifest is None:
                return None
            path = self._snapshot_path(int(manifest["active_version"]))
            document = load_json_checked(path)
            snapshot = ConfigSnapshot.from_document(document)
            if snapshot.version != int(manifest["active_version"]):
                raise RopeError(
                    E_CACHE_CORRUPT,
                    "active snapshot pointer does not match snapshot version",
                    details={
                        "manifest_version": manifest["active_version"],
                        "snapshot_version": snapshot.version,
                    },
                )
            if snapshot.checksum != manifest["active_checksum"]:
                raise RopeError(
                    E_CACHE_CORRUPT,
                    "active snapshot pointer checksum does not match snapshot",
                )
            return snapshot

    def commit(self, snapshot: ConfigSnapshot) -> tuple[ConfigSnapshot, bool]:
        """Commit a complete snapshot; returns (active, changed)."""

        with FileLock(self.lock_path):
            current = self._read_manifest()
            if current is not None:
                current_version = int(current["active_version"])
                if snapshot.version < current_version:
                    raise RopeError(
                        E_VERSION_STALE,
                        "snapshot version is older than the active version",
                        details={
                            "incoming_version": snapshot.version,
                            "active_version": current_version,
                        },
                    )
                if snapshot.version == current_version:
                    if snapshot.checksum == current["active_checksum"]:
                        return self._load_snapshot_locked(snapshot.version), False
                    raise RopeError(
                        E_SNAPSHOT_CONFLICT,
                        "same snapshot version cannot be replaced with different content",
                        details={
                            "version": snapshot.version,
                            "incoming_checksum": snapshot.checksum,
                            "active_checksum": current["active_checksum"],
                        },
                    )
                if snapshot.version <= current_version:
                    raise RopeError(
                        E_VERSION_STALE,
                        "snapshot version must increase monotonically",
                        details={
                            "incoming_version": snapshot.version,
                            "active_version": current_version,
                        },
                    )
            path = self._snapshot_path(snapshot.version)
            if path.exists():
                existing = ConfigSnapshot.from_document(load_json_checked(path))
                if existing.checksum != snapshot.checksum:
                    raise RopeError(
                        E_SNAPSHOT_CONFLICT,
                        "snapshot file already exists with different content",
                        details={"path": str(path)},
                    )
            else:
                atomic_write_bytes(path, snapshot.canonical_bytes())
            manifest_body = {
                "active_checksum": snapshot.checksum,
                "active_version": snapshot.version,
                "schema": "rope-snapshot-manifest/v1",
            }
            manifest = dict(manifest_body)
            manifest["checksum"] = sha256_json(manifest_body)
            atomic_write_bytes(
                self.manifest_path,
                canonical_json_bytes(manifest, pretty=True) + b"\n",
            )
            return snapshot, True

    def _load_snapshot_locked(self, version: int) -> ConfigSnapshot:
        return ConfigSnapshot.from_document(
            load_json_checked(self._snapshot_path(version))
        )

    def next_version(self) -> int:
        active = self.active()
        return 1 if active is None else active.version + 1

    def ensure_current(self, snapshot: ConfigSnapshot) -> ConfigSnapshot:
        active = self.active()
        if active is None:
            raise RopeError(
                E_VERSION_STALE,
                "no active snapshot exists",
                details={"requested_version": snapshot.version},
            )
        if active.version != snapshot.version or active.checksum != snapshot.checksum:
            raise RopeError(
                E_VERSION_STALE,
                "snapshot changed after it was loaded; reload the whole snapshot",
                details={
                    "requested_version": snapshot.version,
                    "requested_checksum": snapshot.checksum,
                    "active_version": active.version,
                    "active_checksum": active.checksum,
                },
            )
        return active

    def replace_from_text(
        self,
        text: str | bytes,
        *,
        source_name: str,
        version: int | None = None,
    ) -> tuple[ConfigSnapshot, bool]:
        parsed = parse_config(text)
        attempts = 6 if version is None else 1
        for attempt in range(attempts):
            snapshot = ConfigSnapshot.create(
                parsed,
                version=version if version is not None else self.next_version(),
                source_name=source_name,
            )
            try:
                return self.commit(snapshot)
            except RopeError as exc:
                if (
                    version is None
                    and exc.code == E_SNAPSHOT_CONFLICT
                    and attempt + 1 < attempts
                ):
                    continue
                raise
        raise AssertionError("snapshot retry loop exhausted")

