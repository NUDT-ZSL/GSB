"""Versioned, atomic snapshots plus workspace state and progress records."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .formats import (
    atomic_write_json,
    build_sidecar,
    content_hash,
    sidecar_file_hash,
    transcript_fingerprint,
)
from .models import Transcript
from .timeutil import PrecisionStats


class SnapshotError(RuntimeError):
    pass


@dataclass(slots=True)
class Snapshot:
    version: int
    transcript: Transcript
    sidecar: dict[str, Any]
    precision: PrecisionStats
    operation_log: list[dict[str, Any]] = field(default_factory=list)
    ir_hash: str = ""
    sidecar_hash: str = ""
    operation_log_digest: str = ""
    plan_digest: str | None = None
    last_completed_operation: int = 0

    def finalize(self) -> Snapshot:
        self.ir_hash = transcript_fingerprint(self.transcript)
        self.sidecar_hash = sidecar_file_hash(self.sidecar)
        self.operation_log_digest = content_hash(self.operation_log)
        return self

    def validate(self) -> None:
        if self.version < 0:
            raise SnapshotError("snapshot version must be non-negative")
        if self.ir_hash != transcript_fingerprint(self.transcript):
            raise SnapshotError("snapshot IR fingerprint mismatch")
        if self.sidecar_hash != sidecar_file_hash(self.sidecar):
            raise SnapshotError("snapshot sidecar hash mismatch")
        if self.operation_log_digest != content_hash(self.operation_log):
            raise SnapshotError("snapshot operation-log digest mismatch")
        if self.sidecar.get("v2_content_hash") != build_sidecar(self.transcript).get(
            "v2_content_hash"
        ):
            raise SnapshotError("snapshot sidecar does not match snapshot IR")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "asr-interop-snapshot",
            "snapshot_version": self.version,
            "last_completed_operation": self.last_completed_operation,
            "plan_digest": self.plan_digest,
            "ir_hash": self.ir_hash,
            "sidecar_hash": self.sidecar_hash,
            "operation_log_digest": self.operation_log_digest,
            "precision": self.precision.to_dict(),
            "ir": self.transcript.to_dict(),
            "sidecar": self.sidecar,
            "operation_log": self.operation_log,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        if (
            data.get("schema_version") != 1
            or data.get("kind") != "asr-interop-snapshot"
        ):
            raise SnapshotError("unsupported snapshot schema")
        try:
            snapshot = cls(
                version=int(data["snapshot_version"]),
                transcript=Transcript.from_dict(data["ir"]),
                sidecar=dict(data["sidecar"]),
                precision=PrecisionStats.from_dict(data.get("precision")),
                operation_log=list(data.get("operation_log", [])),
                ir_hash=str(data.get("ir_hash", "")),
                sidecar_hash=str(data.get("sidecar_hash", "")),
                operation_log_digest=str(data.get("operation_log_digest", "")),
                plan_digest=data.get("plan_digest"),
                last_completed_operation=int(data.get("last_completed_operation", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotError(f"malformed snapshot: {exc}") from exc
        snapshot.validate()
        return snapshot


class SnapshotStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.snapshot_dir = self.state_dir / "snapshots"
        self.latest_path = self.snapshot_dir / "snapshot.json"
        self.state_path = self.state_dir / "workspace.json"
        self.progress_path = self.state_dir / "progress.json"

    def snapshot_path(self, version: int) -> Path:
        return self.snapshot_dir / f"snapshot-{version:06d}.json"

    def write_snapshot(self, snapshot: Snapshot) -> None:
        snapshot.finalize()
        payload = snapshot.to_dict()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.snapshot_path(snapshot.version), payload)
        atomic_write_json(self.latest_path, payload)

    def _read_snapshot(self, path: Path) -> Snapshot:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise SnapshotError(f"snapshot is not UTF-8: {path}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SnapshotError(
                f"invalid snapshot JSON {path}: line {exc.lineno}: {exc.msg}"
            ) from exc
        if not isinstance(data, dict):
            raise SnapshotError(f"snapshot must be an object: {path}")
        return Snapshot.from_dict(data)

    def load_latest(self) -> Snapshot | None:
        candidates: list[tuple[int, Path]] = []
        if self.latest_path.exists():
            candidates.append((-1, self.latest_path))
        if self.snapshot_dir.exists():
            for candidate in self.snapshot_dir.glob("snapshot-*.json"):
                match = re.fullmatch(r"snapshot-(\d+)\.json", candidate.name)
                if match:
                    candidates.append((int(match.group(1)), candidate))
        # Prefer the highest complete version even if a crash happened after
        # writing the versioned file but before replacing snapshot.json.
        candidates.sort(key=lambda item: item[0], reverse=True)
        saw_candidate = bool(candidates)
        for _, candidate in candidates:
            try:
                return self._read_snapshot(candidate)
            except SnapshotError:
                continue
        if saw_candidate:
            raise SnapshotError("all snapshot candidates are corrupt")
        return None

    def read_state(self) -> dict[str, Any] | None:
        return self._read_optional_json(self.state_path)

    def read_progress(self) -> dict[str, Any] | None:
        return self._read_optional_json(self.progress_path)

    def write_state(
        self,
        snapshot: Snapshot,
        plan_digest: str | None = None,
        last_completed_operation: int | None = None,
    ) -> None:
        completed = (
            snapshot.last_completed_operation
            if last_completed_operation is None
            else last_completed_operation
        )
        state = {
            "schema_version": 1,
            "kind": "asr-interop-workspace-state",
            "workspace_version": snapshot.version,
            "snapshot_version": snapshot.version,
            "last_completed_operation": completed,
            "operation_plan_digest": plan_digest,
            "ir_hash": snapshot.ir_hash,
            "sidecar_hash": snapshot.sidecar_hash,
            "v2_content_hash": snapshot.sidecar.get("v2_content_hash"),
            "operation_log_digest": snapshot.operation_log_digest,
        }
        atomic_write_json(self.state_path, state)

    def write_progress(
        self,
        snapshot: Snapshot,
        plan_digest: str | None = None,
        last_completed_operation: int | None = None,
    ) -> None:
        completed = (
            snapshot.last_completed_operation
            if last_completed_operation is None
            else last_completed_operation
        )
        progress = {
            "schema_version": 1,
            "kind": "asr-interop-progress",
            "last_completed_operation": completed,
            "snapshot_version": snapshot.version,
            "operation_plan_digest": plan_digest,
            "ir_hash": snapshot.ir_hash,
            "updated_after_atomic_snapshot": True,
        }
        atomic_write_json(self.progress_path, progress)

    def _read_optional_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"cannot read {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotError(f"{path} must contain a JSON object")
        return data


def plan_digest(operations: list[dict[str, Any]]) -> str:
    return content_hash(operations)
