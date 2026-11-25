"""Versioned, checksummed, atomically replaced JSON ledgers."""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .errors import ELedgerCorrupt, EStageBusy
from .util import atomic_write_bytes, canonical_bytes, read_json, sha256_obj


FORMAT_VERSION = 1


def snapshot_checksum(version: int, data: Any) -> str:
    return sha256_obj({"format_version": FORMAT_VERSION, "version": version, "data": data})


def make_snapshot(version: int, data: Any) -> dict[str, Any]:
    payload = {
        "format_version": FORMAT_VERSION,
        "version": version,
        "data": data,
    }
    return {**payload, "checksum": snapshot_checksum(version, data)}


def verify_snapshot(value: dict[str, Any], source: str = "<memory>") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ELedgerCorrupt("ledger is not an object", {"path": source})
    required = {"format_version", "version", "data", "checksum"}
    absent = sorted(required - value.keys())
    if absent:
        raise ELedgerCorrupt("ledger header is incomplete", {"path": source, "missing": absent})
    if value["format_version"] != FORMAT_VERSION:
        raise ELedgerCorrupt(
            "unknown ledger format version",
            {"path": source, "found": value["format_version"]},
        )
    version = value["version"]
    if not isinstance(version, int) or version <= 0:
        raise ELedgerCorrupt("ledger version must be a positive integer", {"path": source})
    expected_checksum = snapshot_checksum(version, value["data"])
    if value["checksum"] != expected_checksum:
        raise ELedgerCorrupt(
            "ledger checksum mismatch",
            {
                "path": source,
                "declared": value["checksum"],
                "computed": expected_checksum,
            },
        )
    return value


class CommitGuard(AbstractContextManager["CommitGuard"]):
    """Non-blocking exclusive lock implemented with O_EXCL.

    A crash can leave a lock file behind. If the recorded PID is no longer
    alive, the guard removes that stale lock once and retries immediately.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._held = False

    def _try_acquire(self) -> bool:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid()}))
            handle.flush()
            os.fsync(handle.fileno())
        self._held = True
        return True

    def _remove_if_stale(self) -> bool:
        try:
            payload = read_json(self.path)
            owner_pid = int(payload.get("pid", -1))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if owner_pid <= 0:
            return False
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            self.path.unlink(missing_ok=True)
            return True
        except PermissionError:
            return False
        return False

    def __enter__(self) -> "CommitGuard":
        if self._try_acquire():
            return self
        if self._remove_if_stale() and self._try_acquire():
            return self
        raise EStageBusy(
            "another commit currently owns this stage",
            {"lock": str(self.path)},
        )

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False


class VersionedLedger:
    """A JSON ledger whose every commit replaces the full snapshot atomically."""

    def __init__(self, root: str | Path, name: str) -> None:
        self.root = Path(root)
        self.name = name
        self.path = self.root / f"{name}.json"
        self.lock_path = self.root / f".{name}.commit.lock"

    def read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        value = read_json(self.path)
        return verify_snapshot(value, str(self.path))

    def version_and_checksum(self) -> tuple[int, str] | None:
        current = self.read()
        if current is None:
            return None
        return current["version"], current["checksum"]

    def commit(
        self,
        data: Any,
        *,
        expected_version: int | None,
        expected_checksum: str | None,
    ) -> dict[str, Any]:
        with CommitGuard(self.lock_path):
            current = self.read()
            if current is None:
                if expected_version is not None:
                    raise EStageBusy(
                        "ledger disappeared or was replaced before commit",
                        {"ledger": self.name, "expected_version": expected_version},
                    )
                new_version = 1
            else:
                if expected_version is None:
                    raise EStageBusy(
                        "ledger appeared before commit",
                        {"ledger": self.name, "actual_version": current["version"]},
                    )
                if current["version"] != expected_version:
                    raise EStageBusy(
                        "ledger version changed; full snapshot replacement refused",
                        {
                            "ledger": self.name,
                            "expected_version": expected_version,
                            "actual_version": current["version"],
                        },
                    )
                if expected_checksum is not None and current["checksum"] != expected_checksum:
                    raise EStageBusy(
                        "ledger checksum changed; full snapshot replacement refused",
                        {
                            "ledger": self.name,
                            "expected_checksum": expected_checksum,
                            "actual_checksum": current["checksum"],
                        },
                    )
                new_version = current["version"] + 1
            snapshot = make_snapshot(new_version, data)
            atomic_write_bytes(self.path, canonical_bytes(snapshot) + b"\n")
            return snapshot

    def replace_if_changed(
        self,
        data: Any,
        *,
        identity: str,
    ) -> tuple[dict[str, Any], bool]:
        """Replace the full snapshot when the source identity changed."""

        current = self.read()
        if current is not None and current["data"].get("source_identity") == identity:
            return current, False
        if current is None:
            snapshot = self.commit(data, expected_version=None, expected_checksum=None)
            return snapshot, True
        snapshot = self.commit(
            data,
            expected_version=current["version"],
            expected_checksum=current["checksum"],
        )
        return snapshot, True
