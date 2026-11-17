"""Generation-directory snapshots with an atomically replaced CURRENT pointer."""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .errors import SnapshotInvalidError
from .jsonutil import (
    atomic_write_json,
    dumps_canonical,
    loads_decimal,
    read_json,
    sha256_text,
    stable_hash,
)
from .model import ProjectState


SNAPSHOT_SCHEMA = "domain-editor-snapshot/1"


class SnapshotStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace)
        self.root = self.workspace / "snapshots"
        self.current_path = self.root / "CURRENT"
        self.progress_path = self.workspace / "progress.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def reset(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.progress_path.unlink()
        except FileNotFoundError:
            pass

    def _load_current_pointer(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            value = read_json(self.current_path)
        except Exception as exc:
            raise SnapshotInvalidError(f"cannot read CURRENT: {exc}") from exc
        if not isinstance(value, dict) or "generation" not in value:
            raise SnapshotInvalidError("CURRENT does not contain a generation")
        return value

    def load(self) -> tuple[dict[str, Any], dict[str, Any]] | None:
        pointer = self._load_current_pointer()
        if pointer is None:
            return None
        snapshot_path = self.root / str(pointer["generation"]) / "snapshot.json"
        try:
            snapshot = read_json(snapshot_path)
        except Exception as exc:
            raise SnapshotInvalidError(f"cannot read snapshot {snapshot_path.name}: {exc}") from exc
        if not isinstance(snapshot, dict):
            raise SnapshotInvalidError("snapshot must be an object")
        if snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise SnapshotInvalidError("snapshot schema mismatch")
        body = dict(snapshot)
        claimed_hash = body.pop("snapshot_hash", None)
        if not claimed_hash or stable_hash(body) != claimed_hash:
            raise SnapshotInvalidError("snapshot integrity hash mismatch")
        if pointer.get("snapshot_hash") not in (None, claimed_hash):
            raise SnapshotInvalidError("CURRENT pointer and snapshot hash disagree")
        state = ProjectState.from_dict(snapshot["state"])
        if state.ir_fingerprint != snapshot.get("ir_fingerprint"):
            raise SnapshotInvalidError("snapshot IR fingerprint mismatch")
        if state.loss_hash != snapshot.get("loss_hash"):
            raise SnapshotInvalidError("snapshot loss hash mismatch")
        if state.degradation_fingerprint != snapshot.get("degradation_fingerprint"):
            raise SnapshotInvalidError("snapshot degradation fingerprint mismatch")
        if state.global_version != int(snapshot.get("snapshot_version", -1)):
            raise SnapshotInvalidError("snapshot version and state global_version disagree")
        return snapshot, pointer

    def write(
        self,
        state: ProjectState,
        *,
        artifact: dict[str, Any],
        operation_index: int,
        command_id: str | None,
        status: str = "running",
    ) -> dict[str, Any]:
        state.canonicalize()
        body = {
            "schema": SNAPSHOT_SCHEMA,
            "snapshot_version": state.global_version,
            "global_version": state.global_version,
            "source_version": state.source_version,
            "source_fingerprint": state.source_fingerprint,
            "target_version": state.target_version,
            "ir_fingerprint": state.ir_fingerprint,
            "loss_hash": state.loss_hash,
            "degradation_fingerprint": state.degradation_fingerprint,
            "last_operation_index": operation_index,
            "last_command_id": command_id,
            "artifact": artifact,
            "state": state.to_dict(),
        }
        snapshot_hash = stable_hash(body)
        snapshot = dict(body)
        snapshot["snapshot_hash"] = snapshot_hash
        generation = (
            f"gen-{state.global_version:08d}-op{operation_index:06d}-{snapshot_hash[:12]}"
        )
        generation_dir = self.root / generation
        temporary = Path(tempfile.mkdtemp(prefix=".snapshot-tmp-", dir=self.root))
        try:
            atomic_write_json(temporary / "snapshot.json", snapshot)
            if generation_dir.exists():
                shutil.rmtree(temporary)
            else:
                os.replace(temporary, generation_dir)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise
        pointer = {
            "generation": generation,
            "snapshot_version": state.global_version,
            "snapshot_hash": snapshot_hash,
            "last_operation_index": operation_index,
        }
        atomic_write_json(self.current_path, pointer)
        self._cleanup(generation)
        self.write_progress(
            last_completed_operation=operation_index,
            snapshot_version=state.global_version,
            source_version=state.source_version,
            target_version=state.target_version,
            last_command_id=command_id,
            status=status,
        )
        return pointer

    def _cleanup(self, keep_generation: str) -> None:
        directories = sorted(
            [path for path in self.root.iterdir() if path.is_dir() and path.name.startswith("gen-")],
            key=lambda path: path.name,
            reverse=True,
        )
        kept = 0
        for directory in directories:
            if directory.name == keep_generation:
                kept += 1
                continue
            if kept < 2:
                kept += 1
                continue
            shutil.rmtree(directory, ignore_errors=True)

    def write_progress(
        self,
        *,
        last_completed_operation: int,
        snapshot_version: int,
        source_version: str,
        target_version: str,
        last_command_id: str | None,
        status: str,
    ) -> None:
        atomic_write_json(
            self.progress_path,
            {
                "last_completed_operation": last_completed_operation,
                "snapshot_version": snapshot_version,
                "source_version": source_version,
                "target_version": target_version,
                "last_command_id": last_command_id,
                "status": status,
            },
        )

    def read_progress(self) -> dict[str, Any] | None:
        if not self.progress_path.exists():
            return None
        try:
            value = read_json(self.progress_path)
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def recovery_info(self) -> dict[str, Any] | None:
        try:
            loaded = self.load()
        except SnapshotInvalidError as exc:
            return {"valid": False, "error": str(exc)}
        if loaded is None:
            return None
        snapshot, pointer = loaded
        return {
            "valid": True,
            "generation": pointer.get("generation"),
            "snapshot_version": snapshot.get("snapshot_version"),
            "source_version": snapshot.get("source_version"),
            "source_fingerprint": snapshot.get("source_fingerprint"),
            "target_version": snapshot.get("target_version"),
            "last_operation_index": snapshot.get("last_operation_index"),
            "last_command_id": snapshot.get("last_command_id"),
            "ir_fingerprint": snapshot.get("ir_fingerprint"),
            "loss_hash": snapshot.get("loss_hash"),
            "artifact_sha256": snapshot.get("artifact", {}).get("sha256"),
            "progress": self.read_progress(),
        }
