"""Atomic checkpoint file store with crash-artifact detection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .codes import E_RECOVERY_FAILED, E_SNAPSHOT_CORRUPT
from .errors import raise_business


@dataclass(frozen=True)
class CheckpointLoadResult:
    bundle: dict[str, Any] | None
    discarded_pending: bool
    pending_payload: dict[str, Any] | None


class CheckpointStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "checkpoint.json"
        self.pending_path = self.directory / "checkpoint.json.pending"

    @property
    def path_text(self) -> str:
        return str(self.path)

    def save(self, bundle: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = canonical_json_bytes(bundle) + b"\n"
        temp_path = self.directory / ".checkpoint.json.tmp"
        with open(temp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.path)
        self._fsync_directory()

    def load(self) -> CheckpointLoadResult:
        pending_payload: dict[str, Any] | None = None
        discarded = False
        if self.pending_path.exists():
            discarded = True
            try:
                pending_payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending_payload = {"unreadable": True}
            try:
                self.pending_path.unlink()
                self._fsync_directory()
            except OSError:
                pass
        if not self.path.exists():
            return CheckpointLoadResult(None, discarded, pending_payload)
        try:
            raw = self.path.read_bytes()
            bundle = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise_business(
                E_SNAPSHOT_CORRUPT,
                case_id=None,
                segment="checkpoint.file",
                message=f"checkpoint cannot be decoded: {exc}",
                snapshot_category="checkpoint",
                snapshot_version=None,
            )
        if not isinstance(bundle, dict):
            raise_business(
                E_SNAPSHOT_CORRUPT,
                case_id=None,
                segment="checkpoint.file",
                message="checkpoint root must be an object",
                snapshot_category="checkpoint",
                snapshot_version=None,
            )
        return CheckpointLoadResult(bundle, discarded, pending_payload)

    def write_uncommitted_artifact(self, payload: dict[str, Any]) -> None:
        """Leave a deliberately uncommitted side file for crash-recovery tests."""

        self.directory.mkdir(parents=True, exist_ok=True)
        data = canonical_json_bytes(payload) + b"\n"
        with open(self.pending_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def require_bundle(self) -> CheckpointLoadResult:
        result = self.load()
        if result.bundle is None:
            raise_business(
                E_RECOVERY_FAILED,
                case_id=None,
                segment="checkpoint",
                message="resume requested but no committed checkpoint exists",
            )
        return result

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
