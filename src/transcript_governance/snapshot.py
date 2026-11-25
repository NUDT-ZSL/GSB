"""Versioned snapshots with checksums and non-blocking commit control."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codes import Code, Diagnostic
from .hashing import digest_json
from .models import Snapshot, Utterance


def snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "utterances": [item.to_dict() for item in snapshot.utterances],
        "config": snapshot.config,
        "declared_regions": [list(item) for item in snapshot.declared_regions],
        "pcm_hash": snapshot.pcm_hash,
        "transcript_digests": snapshot.transcript_digests,
    }


def compute_snapshot_checksum(
    *,
    version: int,
    utterances: list[Utterance] | tuple[Utterance, ...],
    config: dict[str, Any],
    declared_regions: list[tuple[int, int]] | tuple[tuple[int, int], ...] = (),
    pcm_hash: str | None = None,
    transcript_digests: dict[str, str] | None = None,
) -> str:
    return digest_json(
        {
            "version": version,
            "utterances": [item.to_dict() for item in utterances],
            "config": config,
            "declared_regions": [list(item) for item in declared_regions],
            "pcm_hash": pcm_hash,
            "transcript_digests": transcript_digests or {},
        }
    )


def build_snapshot(
    version: int,
    utterances: list[Utterance] | tuple[Utterance, ...],
    *,
    config: dict[str, Any] | None = None,
    declared_regions: list[tuple[int, int]] | tuple[tuple[int, int], ...] = (),
    pcm_hash: str | None = None,
    transcript_digests: dict[str, str] | None = None,
) -> Snapshot:
    checksum = compute_snapshot_checksum(
        version=version,
        utterances=utterances,
        config=config or {},
        declared_regions=declared_regions,
        pcm_hash=pcm_hash,
        transcript_digests=transcript_digests,
    )
    return Snapshot(
        version=version,
        checksum=checksum,
        utterances=tuple(utterances),
        config=config or {},
        declared_regions=tuple(declared_regions),
        pcm_hash=pcm_hash,
        transcript_digests=transcript_digests or {},
    )


@dataclass
class CommitResult:
    ok: bool
    code: str = Code.OK
    snapshot: Snapshot | None = None
    diagnostic: Diagnostic | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "snapshot_version": self.snapshot.version if self.snapshot else None,
            "snapshot_checksum": self.snapshot.checksum if self.snapshot else None,
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
        }


@dataclass
class StageAttempt:
    ok: bool
    code: str
    lease: "StageLease | None" = None
    diagnostic: Diagnostic | None = None


class StageLease:
    def __init__(self, store: "SnapshotStore", lock_fd: int, token: str) -> None:
        self.store = store
        self.lock_fd = lock_fd
        self.token = token
        self.released = False

    def commit(
        self,
        candidate: Snapshot,
        *,
        expected_version: int,
        expected_checksum: str,
    ) -> CommitResult:
        if self.released:
            return CommitResult(False, Code.E_STAGE_NOT_FOUND, diagnostic=Diagnostic(Code.E_STAGE_NOT_FOUND, "stage lease is already released"))
        current = self.store.load()
        if current is None:
            if expected_version != 0 or expected_checksum not in ("", "GENESIS"):
                return self._fail(
                    Code.E_SNAPSHOT_STALE,
                    "snapshot base does not exist; reload and replace the complete snapshot",
                    expected_version=expected_version,
                )
        else:
            if current.version != expected_version:
                return self._fail(
                    Code.E_SNAPSHOT_VERSION_CONFLICT,
                    "snapshot version changed; reload and replace the complete snapshot",
                    expected=expected_version,
                    actual=current.version,
                )
            if current.checksum != expected_checksum:
                return self._fail(
                    Code.E_SNAPSHOT_CHECKSUM_MISMATCH,
                    "snapshot checksum changed; reload and replace the complete snapshot",
                    expected=expected_checksum,
                    actual=current.checksum,
                )

        expected_checksum_value = compute_snapshot_checksum(
            version=candidate.version,
            utterances=candidate.utterances,
            config=candidate.config,
            declared_regions=candidate.declared_regions,
            pcm_hash=candidate.pcm_hash,
            transcript_digests=candidate.transcript_digests,
        )
        if candidate.checksum != expected_checksum_value:
            return self._fail(
                Code.E_SNAPSHOT_CHECKSUM_MISMATCH,
                "candidate snapshot checksum does not match its content",
                expected=expected_checksum_value,
                actual=candidate.checksum,
            )
        if candidate.version != expected_version + 1:
            return self._fail(
                Code.E_SNAPSHOT_VERSION_CONFLICT,
                "candidate version must be exactly one greater than base version",
                expected=expected_version + 1,
                actual=candidate.version,
            )

        self.store._atomic_write(candidate)
        self.release()
        return CommitResult(True, Code.OK, snapshot=candidate)

    def _fail(self, code: str, message: str, **details: Any) -> CommitResult:
        diagnostic = Diagnostic(code, message, details=details)
        self.release()
        return CommitResult(False, code, diagnostic=diagnostic)

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self.lock_fd)

    def __enter__(self) -> "StageLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.directory / "snapshot.json"
        self.lock_path = self.directory / "stage.lock"

    def exists(self) -> bool:
        return self.snapshot_path.exists()

    def initialize(self, snapshot: Snapshot) -> Snapshot:
        if snapshot.version != 1:
            raise ValueError("initial snapshot version must be 1")
        actual = compute_snapshot_checksum(
            version=snapshot.version,
            utterances=snapshot.utterances,
            config=snapshot.config,
            declared_regions=snapshot.declared_regions,
            pcm_hash=snapshot.pcm_hash,
            transcript_digests=snapshot.transcript_digests,
        )
        if actual != snapshot.checksum:
            raise ValueError("initial snapshot checksum is invalid")
        self._atomic_write(snapshot)
        return snapshot

    def load(self) -> Snapshot | None:
        if not self.snapshot_path.exists():
            return None
        data = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        return Snapshot(
            version=int(data["version"]),
            checksum=str(data["checksum"]),
            utterances=tuple(Utterance.from_dict(item) for item in data.get("utterances", [])),
            config=dict(data.get("config", {})),
            declared_regions=tuple((int(item[0]), int(item[1])) for item in data.get("declared_regions", [])),
            pcm_hash=data.get("pcm_hash"),
            transcript_digests=dict(data.get("transcript_digests", {})),
        )

    def try_stage(self) -> StageAttempt:
        lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(lock_fd)
            return StageAttempt(
                False,
                Code.E_STAGE_BUSY,
                diagnostic=Diagnostic(
                    Code.E_STAGE_BUSY,
                    "another snapshot stage is active; retry after reloading",
                    details={"errno": getattr(exc, "errno", None)},
                ),
            )
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
        return StageAttempt(True, Code.OK, lease=StageLease(self, lock_fd, f"pid-{os.getpid()}"))

    def commit(
        self,
        candidate: Snapshot,
        *,
        expected_version: int,
        expected_checksum: str,
    ) -> CommitResult:
        """Convenience one-shot commit; returns ``E_STAGE_BUSY`` without waiting."""

        attempt = self.try_stage()
        if not attempt.ok or attempt.lease is None:
            return CommitResult(False, Code.E_STAGE_BUSY, diagnostic=attempt.diagnostic)
        return attempt.lease.commit(
            candidate,
            expected_version=expected_version,
            expected_checksum=expected_checksum,
        )

    def _atomic_write(self, snapshot: Snapshot) -> None:
        payload = {"checksum": snapshot.checksum, **snapshot_payload(snapshot)}
        fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-", suffix=".json", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.snapshot_path)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
