"""Persistent four-stage snapshots with atomic replacement and rollback guards."""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from .canonical import atomic_write_json, sha256_value
from .errors import BusinessError
from .models import STAGES


STAGE_FILE_NAMES = {
    stage: f"{index + 1:02d}_{stage}.json" for index, stage in enumerate(STAGES)
}


class SnapshotManager:
    def __init__(
        self, root: Path, expected_sessions: Sequence[str], run_signature: str
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.expected_sessions = sorted(expected_sessions)
        self.run_signature = run_signature
        self.lock_path = self.root / ".stage.lock"

    def _path(self, stage: str) -> Path:
        return self.root / STAGE_FILE_NAMES[stage]

    def remove_orphan_temp_files(self) -> List[str]:
        removed = []
        for path in self.root.glob(".*.tmp"):
            try:
                path.unlink()
                removed.append(path.name)
            except FileNotFoundError:
                pass
        return sorted(removed)

    def _validate_envelope(
        self, envelope: Mapping[str, Any], *, expected_stage: str
    ) -> None:
        stage = envelope.get("stage")
        version = envelope.get("version")
        try:
            checksum = envelope["checksum"]
            base = {key: value for key, value in envelope.items() if key != "checksum"}
            recomputed = sha256_value(base)
        except (KeyError, TypeError):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot envelope is missing checksum fields",
                stage=str(stage or expected_stage),
                version=version if isinstance(version, int) else None,
            )
        if stage != expected_stage:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                f"snapshot stage mismatch: expected {expected_stage}, got {stage}",
                stage=expected_stage,
                version=version if isinstance(version, int) else None,
            )
        if not isinstance(version, int) or version <= 0:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                f"snapshot has invalid version {version!r}",
                stage=expected_stage,
                details={"invalid_version": version},
            )
        if checksum != recomputed:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot checksum mismatch",
                stage=expected_stage,
                version=version,
                details={"expected": recomputed, "actual": checksum},
            )
        completed = envelope.get("completed_sessions")
        if not isinstance(completed, list) or sorted(completed) != self.expected_sessions:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot completed_sessions does not match the full session set",
                stage=expected_stage,
                version=version,
                details={
                    "expected_sessions": self.expected_sessions,
                    "completed_sessions": completed,
                },
            )
        if envelope.get("run_signature") != self.run_signature:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot run signature does not match current input",
                stage=expected_stage,
                version=version,
                details={
                    "expected": self.run_signature,
                    "actual": envelope.get("run_signature"),
                },
            )

    def _read(self, stage: str) -> Optional[Dict[str, Any]]:
        path = self._path(stage)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                f"cannot read snapshot for stage {stage}: {exc}",
                stage=stage,
            ) from exc
        if not isinstance(envelope, dict):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "snapshot envelope must be a JSON object",
                stage=stage,
            )
        self._validate_envelope(envelope, expected_stage=stage)
        return envelope

    def load(self) -> Dict[str, Dict[str, Any]]:
        snapshots: Dict[str, Dict[str, Any]] = {}
        previous_version = 0
        stage_missing = False
        for stage in STAGES:
            envelope = self._read(stage)
            if envelope is None:
                stage_missing = True
                continue
            if stage_missing:
                raise BusinessError(
                    "E_SNAPSHOT_CORRUPT",
                    f"stage {stage} exists after a missing stage",
                    stage=stage,
                    version=int(envelope["version"]),
                )
            version = int(envelope["version"])
            if version <= previous_version:
                raise BusinessError(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot versions are not monotonically increasing",
                    stage=stage,
                    version=version,
                    details={"previous_version": previous_version},
                )
            previous_version = version
            snapshots[stage] = envelope
        return snapshots

    def latest_version(self) -> int:
        snapshots = self.load()
        return max((int(item["version"]) for item in snapshots.values()), default=0)

    def reset(self) -> None:
        """Remove committed snapshots for a deliberately fresh run."""

        with self.stage_lock("reset"):
            for stage in STAGES:
                try:
                    self._path(stage).unlink()
                except FileNotFoundError:
                    pass
            self.remove_orphan_temp_files()

    def _lock_owner(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _remove_stale_lock(self) -> bool:
        owner = self._lock_owner()
        if owner is None:
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                return False
            if age < 30:
                return False
            try:
                self.lock_path.unlink()
                return True
            except FileNotFoundError:
                return True
        if owner.get("host") != socket.gethostname():
            return False
        pid = owner.get("pid")
        if not isinstance(pid, int):
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                self.lock_path.unlink()
                return True
            except FileNotFoundError:
                return True
        except PermissionError:
            return False
        except OSError:
            return False
        return False

    @contextmanager
    def stage_lock(self, stage: str) -> Iterator[None]:
        token = uuid.uuid4().hex
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        for attempt in range(2):
            try:
                fd = os.open(str(self.lock_path), flags, 0o600)
                break
            except FileExistsError as exc:
                if attempt == 0 and self._remove_stale_lock():
                    continue
                owner = self._lock_owner() or {}
                raise BusinessError(
                    "E_STAGE_BUSY",
                    f"stage {stage} is already being committed",
                    stage=stage,
                    details={"owner": owner},
                ) from exc
        else:
            raise BusinessError(
                "E_STAGE_BUSY",
                f"stage {stage} is already being committed",
                stage=stage,
            )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "token": token,
                        "stage": stage,
                        "acquired_at": time.time(),
                    },
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            yield
        finally:
            owner = self._lock_owner()
            if owner and owner.get("token") == token:
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass

    def commit(
        self,
        stage: str,
        data: Mapping[str, Any],
        completed_sessions: Sequence[str],
        version: Optional[int] = None,
    ) -> Dict[str, Any]:
        if stage not in STAGES:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                f"unknown stage {stage!r}",
                stage=stage,
            )
        if sorted(completed_sessions) != self.expected_sessions:
            raise BusinessError(
                "E_SNAPSHOT_INCOMPLETE",
                "a stage snapshot can only be committed after every session completes",
                stage=stage,
                details={
                    "expected_sessions": self.expected_sessions,
                    "completed_sessions": sorted(completed_sessions),
                },
            )
        with self.stage_lock(stage):
            existing = self.load()
            latest = max(
                (int(item["version"]) for item in existing.values()), default=0
            )
            candidate = latest + 1 if version is None else int(version)
            existing_stage = existing.get(stage)
            existing_stage_version = (
                int(existing_stage["version"]) if existing_stage else 0
            )
            if candidate <= latest or candidate <= existing_stage_version:
                raise BusinessError(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot version must be greater than every committed version",
                    stage=stage,
                    version=candidate,
                    details={
                        "latest_version": latest,
                        "existing_stage_version": existing_stage_version,
                    },
                )
            base = {
                "schema_version": 1,
                "stage": stage,
                "version": candidate,
                "completed_sessions": sorted(completed_sessions),
                "run_signature": self.run_signature,
                "data": dict(data),
            }
            envelope = dict(base)
            envelope["checksum"] = sha256_value(base)
            atomic_write_json(self._path(stage), envelope)
            return envelope
