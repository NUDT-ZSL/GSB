from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .canonical import content_digest
from .errors import E_INCONSISTENT, E_NOT_FOUND, E_STAGE_BUSY, E_VERSION_UNSUPPORTED, TuningError


class CommitLock:
    """Non-blocking cross-process lock for snapshot commit."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def acquire(self) -> "CommitLock":
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise TuningError(
                E_STAGE_BUSY,
                "another ledger snapshot commit is already active",
                {"lock_path": str(self.path)},
            ) from exc
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError:
            pass
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "CommitLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def atomic_write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class SnapshotStore:
    def __init__(self, output_dir: str | Path):
        self.root = Path(output_dir)
        self.checkpoint_dir = self.root / "checkpoints"
        self.ledger_dir = self.root / "ledger"
        self.history_dir = self.ledger_dir / "history"
        self.work_dir = self.root / "work"
        self.staging_dir = self.root / "staging"
        for directory in (
            self.checkpoint_dir,
            self.history_dir,
            self.work_dir,
            self.staging_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.ledger_dir / "commit.lock"
        self.latest_checkpoint = self.checkpoint_dir / "latest.json"
        self.current_ledger = self.ledger_dir / "current.json"

    def versioned_checkpoint(self, version: int) -> Path:
        return self.checkpoint_dir / f"checkpoint-{version:06d}.json"

    def list_versions(self) -> list[int]:
        versions: list[int] = []
        for path in self.checkpoint_dir.glob("checkpoint-*.json"):
            match = re.fullmatch(r"checkpoint-(\d{6})\.json", path.name)
            if match:
                versions.append(int(match.group(1)))
        return sorted(versions)

    def latest_version(self) -> int:
        versions = self.list_versions()
        return versions[-1] if versions else 0

    @staticmethod
    def verify_record(record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise TuningError(E_INCONSISTENT, "snapshot is not an object")
        schema = record.get("schema_version")
        if schema != SCHEMA_VERSION:
            raise TuningError(
                E_VERSION_UNSUPPORTED,
                f"unsupported ledger schema version: {schema}",
            )
        payload = record.get("payload")
        expected = record.get("checksum")
        actual = content_digest(payload)
        if expected != actual:
            raise TuningError(
                E_INCONSISTENT,
                "ledger snapshot checksum does not match canonical payload",
                {"expected": expected, "actual": actual},
            )
        return record

    def load_record(self, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.exists():
            raise TuningError(E_NOT_FOUND, f"snapshot not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            record = json.load(handle)
        return self.verify_record(record)

    def load_latest(self) -> dict[str, Any] | None:
        version = self.latest_version()
        if version == 0:
            if self.latest_checkpoint.exists():
                return self.load_record(self.latest_checkpoint)
            return None
        return self.load_record(self.versioned_checkpoint(version))

    def commit(
        self,
        state: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with CommitLock(self.lock_path):
            disk_version = self.latest_version()
            if expected_version is not None and disk_version != int(expected_version):
                raise TuningError(
                    E_STAGE_BUSY,
                    "snapshot version changed before commit; reload and retry as a new stage",
                    {"expected_version": expected_version, "disk_version": disk_version},
                )
            version = disk_version + 1
            payload = dict(state)
            payload["snapshot_version"] = version
            payload["checkpoint_version"] = version
            checksum = content_digest(payload)
            record = {
                "schema_version": SCHEMA_VERSION,
                "snapshot_version": version,
                "checksum": checksum,
                "payload": payload,
            }

            # The versioned checkpoint is the commit point. os.replace makes it
            # impossible for recovery to observe a half-written checkpoint.
            atomic_write_json(self.versioned_checkpoint(version), record)
            atomic_write_json(self.latest_checkpoint, record)
            atomic_write_json(self.current_ledger, record)
            atomic_write_json(self.history_dir / f"ledger-{version:06d}.json", record)
            return record

    def refresh_if_changed(
        self, current_record: dict[str, Any] | None
    ) -> tuple[dict[str, Any] | None, bool]:
        """Replace the entire in-memory snapshot when version/checksum changed."""
        if not self.current_ledger.exists():
            return current_record, False
        with self.current_ledger.open("r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        identity = (on_disk.get("snapshot_version"), on_disk.get("checksum"))
        current_identity = (
            (current_record or {}).get("snapshot_version"),
            (current_record or {}).get("checksum"),
        )
        if identity == current_identity:
            return current_record, False
        return self.verify_record(on_disk), True
