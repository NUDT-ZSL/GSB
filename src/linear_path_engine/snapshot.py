"""Atomic whole-snapshot persistence with optimistic rollback protection."""

from __future__ import annotations

import fcntl
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STATE,
    E_VERSION_MISMATCH,
    EngineError,
)
from .util import atomic_write_json, canonical_json_bytes, sha256_value


_SNAPSHOT_FORMAT = "linear-path-snapshot-v1"
_MANIFEST_NAME = "manifest.json"
_LOCK_NAME = ".stage-commit.lock"


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    version: int
    stage: str
    stage_detail: str | None
    payload: dict[str, Any]
    payload_checksum: str
    checksum: str
    path: Path


class StageCommitGuard:
    """Non-blocking cross-process exclusive commit guard."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._handle: Any = None

    def __enter__(self) -> "StageCommitGuard":
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / _LOCK_NAME
        self._handle = open(lock_path, "a+b")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise EngineError(
                E_STAGE_BUSY,
                "another stage commit is already in progress",
                stage="snapshot_commit",
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


class SnapshotStore:
    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory = directory
        self.run_id = run_id
        self.manifest_path = directory / _MANIFEST_NAME

    def _corrupt(
        self,
        message: str,
        *,
        stage: str = "snapshot",
        version: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> EngineError:
        return EngineError(
            E_SNAPSHOT_CORRUPT,
            message,
            stage=stage,
            version=version,
            details=details,
        )

    def _read_manifest_raw(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            raw = self.manifest_path.read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            fallback_stage, fallback_version = self._best_effort_metadata()
            raise self._corrupt(
                f"snapshot manifest is unreadable: {exc}",
                stage=fallback_stage,
                version=fallback_version,
            ) from exc
        if not isinstance(manifest, dict):
            fallback_stage, fallback_version = self._best_effort_metadata()
            raise self._corrupt(
                "snapshot manifest is not an object",
                stage=fallback_stage,
                version=fallback_version,
            )
        stage = manifest.get("stage") if isinstance(manifest.get("stage"), str) else "unknown"
        version = manifest.get("version") if isinstance(manifest.get("version"), int) else None
        if manifest.get("format") != _SNAPSHOT_FORMAT:
            raise self._corrupt(
                "snapshot manifest format is unsupported",
                stage=stage,
                version=version,
            )
        if manifest.get("replacement_mode") != "whole":
            raise self._corrupt(
                "snapshot manifest does not declare whole-snapshot replacement",
                stage=stage,
                version=version,
            )
        return manifest

    def _best_effort_metadata(self) -> tuple[str, int | None]:
        candidates = sorted(self.directory.glob("v[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-*.json"))
        for path in reversed(candidates):
            filename_version: int | None
            try:
                filename_version = int(path.name[1:9])
            except ValueError:
                filename_version = None
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return "unknown", filename_version
            if not isinstance(envelope, dict):
                return "unknown", filename_version
            stage = envelope.get("stage")
            version = envelope.get("version")
            if isinstance(stage, str) and isinstance(version, int):
                return stage, version
            return "unknown", filename_version
        return "unknown", None

    def load_current(self) -> Snapshot | None:
        manifest = self._read_manifest_raw()
        if manifest is None:
            return None
        stage = str(manifest.get("stage", "unknown"))
        version_value = manifest.get("version")
        if isinstance(version_value, bool) or not isinstance(version_value, int):
            raise self._corrupt(
                "snapshot manifest version is invalid",
                stage=stage,
                version=None,
            )
        version = version_value
        if manifest.get("run_id") != self.run_id:
            raise EngineError(
                E_VERSION_MISMATCH,
                "snapshot run_id differs from the configured run_id",
                stage=stage,
                version=version,
                details={
                    "snapshot_run_id": manifest.get("run_id"),
                    "configured_run_id": self.run_id,
                },
            )
        snapshot_file = manifest.get("snapshot_file")
        if not isinstance(snapshot_file, str) or not snapshot_file:
            raise self._corrupt(
                "snapshot manifest does not name a snapshot file",
                stage=stage,
                version=version,
            )
        path = self.directory / snapshot_file
        if not path.exists():
            raise self._corrupt(
                "snapshot file referenced by the manifest is missing",
                stage=stage,
                version=version,
                details={"snapshot_file": snapshot_file},
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise self._corrupt(
                f"snapshot file is unreadable: {exc}",
                stage=stage,
                version=version,
                details={"snapshot_file": snapshot_file},
            ) from exc
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._corrupt(
                f"snapshot file is not valid JSON: {exc}",
                stage=stage,
                version=version,
                details={"snapshot_file": snapshot_file},
            ) from exc
        if not isinstance(envelope, dict):
            raise self._corrupt(
                "snapshot envelope is not an object",
                stage=stage,
                version=version,
            )
        if envelope.get("format") != _SNAPSHOT_FORMAT:
            raise self._corrupt(
                "snapshot envelope format is unsupported",
                stage=stage,
                version=version,
            )
        envelope_stage = envelope.get("stage")
        envelope_version = envelope.get("version")
        if envelope_stage != stage or envelope_version != version:
            raise self._corrupt(
                "snapshot version or stage changed relative to the manifest",
                stage=str(envelope_stage) if isinstance(envelope_stage, str) else stage,
                version=envelope_version if isinstance(envelope_version, int) else version,
                details={
                    "manifest_stage": stage,
                    "manifest_version": version,
                    "envelope_stage": envelope_stage,
                    "envelope_version": envelope_version,
                },
            )
        if manifest.get("stage_detail") != envelope.get("stage_detail"):
            raise self._corrupt(
                "snapshot stage detail changed relative to the manifest",
                stage=stage,
                version=version,
                details={
                    "manifest_stage_detail": manifest.get("stage_detail"),
                    "envelope_stage_detail": envelope.get("stage_detail"),
                },
            )
        if envelope.get("run_id") != self.run_id:
            raise EngineError(
                E_VERSION_MISMATCH,
                "snapshot envelope run_id differs from the configured run_id",
                stage=stage,
                version=version,
            )
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise self._corrupt(
                "snapshot payload is missing or not an object",
                stage=stage,
                version=version,
            )
        payload_checksum = envelope.get("payload_checksum")
        if not isinstance(payload_checksum, str):
            raise self._corrupt(
                "snapshot payload checksum is missing",
                stage=stage,
                version=version,
            )
        try:
            actual_payload_checksum = sha256_value(payload)
        except (TypeError, ValueError) as exc:
            raise self._corrupt(
                f"snapshot payload cannot be canonically hashed: {exc}",
                stage=stage,
                version=version,
            ) from exc
        if actual_payload_checksum != payload_checksum:
            raise self._corrupt(
                "snapshot payload checksum mismatch",
                stage=stage,
                version=version,
                details={
                    "expected_payload_checksum": payload_checksum,
                    "actual_payload_checksum": actual_payload_checksum,
                    "snapshot_file": snapshot_file,
                },
            )
        envelope_checksum = envelope.get("envelope_checksum")
        checksum_payload = {
            "format": envelope.get("format"),
            "schema_version": envelope.get("schema_version"),
            "run_id": envelope.get("run_id"),
            "version": envelope_version,
            "stage": envelope_stage,
            "stage_detail": envelope.get("stage_detail"),
            "payload_checksum": payload_checksum,
        }
        actual_envelope_checksum = sha256_value(checksum_payload)
        if not isinstance(envelope_checksum, str) or actual_envelope_checksum != envelope_checksum:
            raise self._corrupt(
                "snapshot envelope checksum mismatch",
                stage=stage,
                version=version,
                details={
                    "expected_envelope_checksum": envelope_checksum,
                    "actual_envelope_checksum": actual_envelope_checksum,
                    "snapshot_file": snapshot_file,
                },
            )
        if manifest.get("payload_checksum") != payload_checksum:
            raise self._corrupt(
                "snapshot manifest payload checksum does not match the envelope",
                stage=stage,
                version=version,
            )
        if manifest.get("snapshot_checksum") != envelope_checksum:
            raise self._corrupt(
                "snapshot manifest envelope checksum does not match the snapshot",
                stage=stage,
                version=version,
            )
        return Snapshot(
            run_id=self.run_id,
            version=version,
            stage=stage,
            stage_detail=(
                str(manifest["stage_detail"]) if manifest.get("stage_detail") is not None else None
            ),
            payload=payload,
            payload_checksum=payload_checksum,
            checksum=envelope_checksum,
            path=path,
        )

    @staticmethod
    def _safe_detail(stage: str, stage_detail: str | None) -> str:
        detail = stage_detail or "none"
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", detail).strip("-")
        return cleaned[:80] or "none"

    def commit(
        self,
        *,
        payload: dict[str, Any],
        stage: str,
        stage_detail: str | None,
        expected_version: int,
        expected_checksum: str | None,
        requested_version: int | None = None,
    ) -> Snapshot:
        with StageCommitGuard(self.directory):
            current = self.load_current()
            actual_version = current.version if current is not None else 0
            actual_checksum = current.checksum if current is not None else None
            if requested_version is not None:
                if requested_version <= actual_version:
                    raise EngineError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot version rollback is forbidden",
                        stage=current.stage if current else stage,
                        version=actual_version,
                        details={"requested_version": requested_version},
                    )
                new_version = requested_version
            else:
                if expected_version != actual_version:
                    if actual_version > expected_version:
                        raise EngineError(
                            E_SNAPSHOT_ROLLBACK,
                            "a newer snapshot is already committed",
                            stage=current.stage if current else stage,
                            version=actual_version,
                            details={"expected_version": expected_version},
                        )
                    raise EngineError(
                        E_SNAPSHOT_CORRUPT,
                        "committed snapshot version is older than expected",
                        stage=current.stage if current else stage,
                        version=actual_version,
                        details={"expected_version": expected_version},
                    )
                if expected_checksum != actual_checksum:
                    raise EngineError(
                        E_SNAPSHOT_ROLLBACK,
                        "committed snapshot checksum changed; whole replacement refused",
                        stage=current.stage if current else stage,
                        version=actual_version,
                        details={
                            "expected_checksum": expected_checksum,
                            "actual_checksum": actual_checksum,
                        },
                    )
                new_version = actual_version + 1

            try:
                payload_checksum = sha256_value(payload)
            except (TypeError, ValueError) as exc:
                raise EngineError(
                    E_STATE,
                    f"refusing to commit non-canonical snapshot payload: {exc}",
                    stage=stage,
                    version=actual_version,
                ) from exc
            envelope_base = {
                "format": _SNAPSHOT_FORMAT,
                "schema_version": 1,
                "run_id": self.run_id,
                "version": new_version,
                "stage": stage,
                "stage_detail": stage_detail,
                "payload_checksum": payload_checksum,
            }
            envelope_checksum = sha256_value(envelope_base)
            envelope = dict(envelope_base)
            envelope["payload"] = payload
            envelope["envelope_checksum"] = envelope_checksum
            snapshot_file = (
                f"v{new_version:08d}-{self._safe_detail(stage, stage_detail)}.json"
            )
            snapshot_path = self.directory / snapshot_file
            atomic_write_json(snapshot_path, envelope)
            manifest = {
                "format": _SNAPSHOT_FORMAT,
                "schema_version": 1,
                "replacement_mode": "whole",
                "run_id": self.run_id,
                "version": new_version,
                "stage": stage,
                "stage_detail": stage_detail,
                "snapshot_file": snapshot_file,
                "payload_checksum": payload_checksum,
                "snapshot_checksum": envelope_checksum,
                "previous_version": actual_version,
                "previous_checksum": actual_checksum,
            }
            atomic_write_json(self.manifest_path, manifest)
            return Snapshot(
                run_id=self.run_id,
                version=new_version,
                stage=stage,
                stage_detail=stage_detail,
                payload=payload,
                payload_checksum=payload_checksum,
                checksum=envelope_checksum,
                path=snapshot_path,
            )

    def request_rollback(self, target_version: int) -> None:
        current = self.load_current()
        raise EngineError(
            E_SNAPSHOT_ROLLBACK,
            "snapshot rollback is not supported; the committed snapshot is immutable",
            stage=current.stage if current else "snapshot",
            version=current.version if current else 0,
            details={"target_version": target_version},
        )


def snapshot_envelope_bytes(payload: dict[str, Any]) -> bytes:
    """Exposed for tooling that needs the canonical representation."""
    return canonical_json_bytes(payload)

