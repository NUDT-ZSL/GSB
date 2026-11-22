"""Versioned, checksummed, whole-payload snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .model import canonical_json

SNAPSHOT_KINDS = ("parameters", "migration", "render")


def _checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    kind: str
    version: int
    checksum: str
    payload: dict[str, Any]
    field_checksums: dict[str, str]

    def envelope(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "field_checksums": self.field_checksums,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class CommitResult:
    snapshot: Snapshot
    reused: bool
    replaced: bool


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._commit_lock = threading.Lock()

    def _path(self, kind: str) -> Path:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind {kind!r}")
        return self.root / f"{kind}.snapshot.json"

    def _corrupt(
        self,
        kind: str,
        message: str,
        *,
        version: int | None,
        fields: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> BusinessError:
        return BusinessError(
            ErrorCode.SNAPSHOT_CORRUPT,
            message,
            stage="snapshot",
            field_path=(fields[0] if fields else kind),
            details={
                "snapshot_kind": kind,
                "version": version,
                "inconsistent_fields": fields or [],
                **(details or {}),
            },
        )

    def load(self, kind: str) -> Snapshot | None:
        path = self._path(kind)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError as exc:
            match = re.search(r'"version"\s*:\s*(\d+)', text)
            version = int(match.group(1)) if match else None
            raise self._corrupt(
                kind,
                f"snapshot JSON is corrupt: {exc.msg}",
                version=version,
                fields=["document"],
                details={"char_offset": exc.pos},
            ) from exc
        if not isinstance(envelope, dict):
            raise self._corrupt(
                kind, "snapshot envelope must be an object", version=None, fields=["document"]
            )
        version_raw = envelope.get("version")
        try:
            version = int(version_raw)
        except (TypeError, ValueError):
            version = None
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise self._corrupt(
                kind,
                "snapshot payload must be an object",
                version=version,
                fields=["payload"],
            )
        stored_checksum = envelope.get("checksum")
        actual_checksum = _checksum(payload)
        stored_fields = envelope.get("field_checksums")
        if not isinstance(stored_fields, dict):
            stored_fields = {}
        actual_fields = {key: _checksum(value) for key, value in payload.items()}
        inconsistent = sorted(
            key
            for key in set(stored_fields) | set(actual_fields)
            if stored_fields.get(key) != actual_fields.get(key)
        )
        if stored_checksum != actual_checksum or inconsistent:
            fields = inconsistent or ["checksum"]
            raise self._corrupt(
                kind,
                "snapshot checksum does not match its payload",
                version=version,
                fields=fields,
                details={
                    "stored_checksum": stored_checksum,
                    "actual_checksum": actual_checksum,
                },
            )
        if envelope.get("kind") != kind:
            raise self._corrupt(
                kind,
                "snapshot kind does not match its storage slot",
                version=version,
                fields=["kind"],
            )
        return Snapshot(
            kind=kind,
            version=version if version is not None else 0,
            checksum=actual_checksum,
            payload=payload,
            field_checksums=actual_fields,
        )

    def commit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        version: int | None = None,
    ) -> CommitResult:
        if not self._commit_lock.acquire(blocking=False):
            raise BusinessError(
                ErrorCode.STAGE_BUSY,
                "another snapshot commit is already in progress",
                stage="snapshot",
                field_path=kind,
                details={"snapshot_kind": kind, "wait_policy": "non_blocking"},
            )
        try:
            current = self.load(kind)
            checksum = _checksum(payload)
            field_checksums = {
                key: _checksum(value) for key, value in payload.items()
            }
            target_version = (
                (current.version + 1) if current is not None else 1
            ) if version is None else int(version)
            if current is not None:
                if target_version < current.version:
                    raise BusinessError(
                        ErrorCode.SNAPSHOT_ROLLBACK,
                        "snapshot version may not move backwards",
                        stage="snapshot",
                        field_path="version",
                        details={
                            "snapshot_kind": kind,
                            "current_version": current.version,
                            "requested_version": target_version,
                        },
                    )
                if target_version == current.version:
                    if checksum == current.checksum:
                        return CommitResult(current, reused=True, replaced=False)
                    inconsistent = sorted(
                        key
                        for key in set(field_checksums) | set(current.field_checksums)
                        if field_checksums.get(key) != current.field_checksums.get(key)
                    )
                    raise self._corrupt(
                        kind,
                        "same snapshot version was submitted with different content",
                        version=target_version,
                        fields=inconsistent or ["checksum"],
                        details={
                            "existing_checksum": current.checksum,
                            "submitted_checksum": checksum,
                        },
                    )
            snapshot = Snapshot(
                kind=kind,
                version=target_version,
                checksum=checksum,
                payload=payload,
                field_checksums=field_checksums,
            )
            path = self._path(kind)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(
                    snapshot.envelope(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temporary, path)
            return CommitResult(snapshot, reused=False, replaced=current is not None)
        finally:
            self._commit_lock.release()

    def report(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for kind in SNAPSHOT_KINDS:
            try:
                snapshot = self.load(kind)
            except BusinessError as error:
                report[kind] = {"error": error.to_dict()}
                continue
            report[kind] = (
                None
                if snapshot is None
                else {
                    "version": snapshot.version,
                    "checksum": snapshot.checksum,
                }
            )
        return report


def parameter_payload(scene_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "parameters-v1",
        "scene": scene_payload,
        "semantic_hash": _checksum(scene_payload),
    }


def migration_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "migration-v1",
        "report": report,
        "semantic_hash": _checksum(report),
    }


def render_payload(render_report: dict[str, Any], files: dict[str, str]) -> dict[str, Any]:
    return {
        "schema": "render-v1",
        "render": render_report,
        "files": dict(sorted(files.items())),
        "output_hash": render_report["panorama_hash"],
        "mask_hash": render_report["mask_hash"],
    }

