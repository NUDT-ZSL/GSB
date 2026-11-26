"""Versioned whole-document stores with non-blocking commit locks."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

from .common import atomic_write_json, canonical_hash, read_json
from .errors import Codes, DomainError


class CommitLock:
    def __init__(self, lock_path: Path):
        self.path = lock_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd: int | None = None

    def acquire_nowait(self) -> int:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise DomainError(
                "another snapshot commit is in progress",
                code=Codes.E_STAGE_BUSY,
                lock=str(self.path),
            ) from exc
        self.fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        return fd

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.acquire_nowait()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


class VersionedStore:
    """A head pointer plus immutable full JSON document versions."""

    def __init__(self, root: str | Path, domain: str):
        self.root = Path(root) / domain
        self.versions_dir = self.root / "versions"
        self.head_path = self.root / "head.json"
        self.lock_path = self.root / "commit.lock"
        self.versions_dir.mkdir(parents=True, exist_ok=True)

    def head(self) -> dict | None:
        if not self.head_path.exists():
            return None
        head = read_json(self.head_path)
        record = read_json(self.versions_dir / head["document"])
        if record.get("checksum") != head.get("checksum"):
            raise DomainError(
                f"{self.root.name} head checksum does not match version record",
                code=Codes.E_SNAPSHOT_ROLLBACK,
                head=head,
            )
        actual = canonical_hash(record.get("document"))
        if actual != record.get("checksum"):
            raise DomainError(
                f"{self.root.name} current document checksum mismatch",
                code=Codes.E_SNAPSHOT_ROLLBACK,
                expected=record.get("checksum"),
                actual=actual,
            )
        return record

    def commit(
        self,
        document: dict,
        *,
        incoming_revision: int | None = None,
        expected_head_version: int | None = None,
    ) -> dict:
        lock = CommitLock(self.lock_path)
        lock.acquire_nowait()
        try:
            current_head = None
            if self.head_path.exists():
                current_head = read_json(self.head_path)
                if expected_head_version is not None and current_head["version"] != expected_head_version:
                    raise DomainError(
                        "commit head version changed before commit",
                        code=Codes.E_STAGE_BUSY,
                        expected_head_version=expected_head_version,
                        actual_head_version=current_head["version"],
                    )

            current_record = self.head() if current_head else None
            current_revision = None
            if current_record:
                current_revision = (current_record.get("document") or {}).get("revision")
                current_revision = current_revision if current_revision is not None else current_record.get("revision")

            revision = incoming_revision
            if revision is None:
                revision = int(document.get("revision", document.get("rule_revision", 1)))
            if current_revision is not None and int(revision) < int(current_revision):
                raise DomainError(
                    "refusing to replace a newer snapshot/ledger with an older revision",
                    code=Codes.E_SNAPSHOT_ROLLBACK,
                    current_revision=current_revision,
                    incoming_revision=revision,
                )

            version = int(current_head["version"]) + 1 if current_head else 1
            checksum = canonical_hash(document)
            record = {
                "domain": self.root.name,
                "version": version,
                "revision": int(revision),
                "checksum": checksum,
                "document": document,
            }
            document_name = f"{self.root.name}-{version:06d}.json"
            record["document_file"] = document_name
            atomic_write_json(self.versions_dir / document_name, record)
            head = {
                "domain": self.root.name,
                "version": version,
                "revision": int(revision),
                "checksum": checksum,
                "document": document_name,
            }
            atomic_write_json(self.head_path, head)
            return record
        finally:
            lock.release()

    def acquire_hold_for_demo(self) -> CommitLock:
        lock = CommitLock(self.lock_path)
        lock.acquire_nowait()
        return lock
