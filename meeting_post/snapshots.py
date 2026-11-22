"""Versioned whole-snapshot persistence with rollback and corruption guards."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import MeetingConfig, canonical_json_bytes, checksum
from .errors import MeetingError

SNAPSHOT_ITEMS = ("segmentation", "separation", "reid")


@dataclass
class SnapshotRecord:
    item: str
    version: int
    checksum: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }


class SnapshotStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, item: str) -> Path:
        self._validate_item(item)
        return self.root / f"{item}.json"

    @staticmethod
    def _validate_item(item: str) -> None:
        if item not in SNAPSHOT_ITEMS:
            raise MeetingError(
                "E_SNAPSHOT_CORRUPT",
                f"unknown snapshot item {item}",
                {"item": item, "version": None},
            )

    def _corrupt(self, item: str, version: int | None, reason: str, **details: Any) -> MeetingError:
        return MeetingError(
            "E_SNAPSHOT_CORRUPT",
            reason,
            {"item": item, "version": version, **details},
        )

    def read(self, item: str) -> SnapshotRecord | None:
        path = self.path_for(item)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise self._corrupt(item, None, "snapshot cannot be read", error=str(exc)) from exc
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            raise self._corrupt(
                item,
                None,
                "snapshot is not valid JSON",
                line=exc.lineno,
                column=exc.colno,
            ) from exc
        if not isinstance(envelope, dict):
            raise self._corrupt(item, None, "snapshot envelope must be an object")
        version = envelope.get("version")
        if not isinstance(version, int) or version < 1:
            raise self._corrupt(item, version, "snapshot version must be a positive integer")
        if envelope.get("item") != item:
            raise self._corrupt(item, version, "snapshot item identity mismatch")
        payload = envelope.get("payload")
        stored_checksum = envelope.get("checksum")
        if not isinstance(payload, dict) or not isinstance(stored_checksum, str):
            raise self._corrupt(item, version, "snapshot envelope is incomplete")
        actual_checksum = checksum(payload)
        if actual_checksum != stored_checksum:
            raise self._corrupt(
                item,
                version,
                "snapshot checksum mismatch",
                stored_checksum=stored_checksum,
                actual_checksum=actual_checksum,
            )
        return SnapshotRecord(
            item=item,
            version=version,
            checksum=actual_checksum,
            payload=payload,
        )

    def commit(
        self,
        item: str,
        payload: dict[str, Any],
        *,
        version: int | None = None,
    ) -> SnapshotRecord:
        self._validate_item(item)
        if not isinstance(payload, dict):
            raise self._corrupt(item, version, "snapshot payload must be an object")
        new_checksum = checksum(payload)
        existing = self.read(item)
        if existing is not None:
            if version is None:
                version = existing.version + 1
            if version < existing.version:
                raise MeetingError(
                    "E_SNAPSHOT_ROLLBACK",
                    "new snapshot version is older than the persisted version",
                    {
                        "item": item,
                        "current_version": existing.version,
                        "attempted_version": version,
                    },
                )
            if version == existing.version:
                if new_checksum == existing.checksum:
                    return existing
                raise self._corrupt(
                    item,
                    version,
                    "same snapshot version cannot carry different content",
                    current_checksum=existing.checksum,
                    attempted_checksum=new_checksum,
                )
        elif version is None:
            version = 1
        elif version < 1:
            raise self._corrupt(item, version, "snapshot version must be positive")

        envelope = {
            "item": item,
            "version": int(version),
            "checksum": new_checksum,
            "payload": payload,
        }
        encoded = canonical_json_bytes(envelope) + b"\n"
        path = self.path_for(item)
        fd, temp_name = tempfile.mkstemp(prefix=f".{item}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return SnapshotRecord(
            item=item,
            version=int(version),
            checksum=new_checksum,
            payload=payload,
        )

    def update_from_config(self, config: MeetingConfig, *, force_version: bool = False) -> dict[str, SnapshotRecord]:
        payloads = {
            "segmentation": config.segmentation_payload(),
            "separation": config.separation_payload(),
            "reid": config.reid_payload(),
        }
        existing_by_item = {item: self.read(item) for item in payloads}
        records: dict[str, SnapshotRecord] = {}
        for item, payload in payloads.items():
            existing = existing_by_item[item]
            if (
                existing is not None
                and existing.checksum == checksum(payload)
                and not force_version
            ):
                records[item] = existing
                continue
            version = None if existing is None else existing.version + 1
            records[item] = self.commit(item, payload, version=version)
        return records

    def combined_digest(self) -> str:
        payload: dict[str, Any] = {}
        for item in SNAPSHOT_ITEMS:
            record = self.read(item)
            if record is None:
                payload[item] = None
            else:
                payload[item] = {
                    "version": record.version,
                    "checksum": record.checksum,
                }
        return checksum(payload)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in SNAPSHOT_ITEMS:
            record = self.read(item)
            if record is None:
                result[item] = None
            else:
                result[item] = {
                    "version": record.version,
                    "checksum": record.checksum,
                    "payload": record.payload,
                }
        return result

    def attempt_rollback(self, item: str, version: int) -> None:
        existing = self.read(item)
        if existing is None:
            raise self._corrupt(item, version, "snapshot does not exist")
        if version < existing.version:
            raise MeetingError(
                "E_SNAPSHOT_ROLLBACK",
                "snapshot rollback is forbidden",
                {
                    "item": item,
                    "current_version": existing.version,
                    "attempted_version": version,
                },
            )
        raise self._corrupt(
            item,
            existing.version,
            "rollback probe did not target an older version",
        )
