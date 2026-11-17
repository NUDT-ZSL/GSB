"""Atomic checkpoints and cross-process work-directory locking."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .constants import CHECKPOINT_STRUCTURE_VERSION
from .diagnostics import Diagnostic, sort_diagnostics
from .util import (
    atomic_write_json,
    canonical_json_bytes,
    cleanup_temp_files,
    sha256_bytes,
    sha256_value,
)


class LockConflict(RuntimeError):
    def __init__(self, workdir: Path, holder: str = "") -> None:
        self.workdir = workdir
        self.holder = holder
        self.diagnostic = Diagnostic(
            code="E_LOCK",
            message="work directory is locked by another writer",
            source=str(workdir / ".authdiag.lock"),
            details={"workdir": str(workdir), "holder": holder},
            severity="error",
        )
        super().__init__(self.diagnostic.message)


class WorkdirLock:
    """Non-blocking exclusive lock that is released by the OS on process death."""

    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.path = self.workdir / ".authdiag.lock"
        self._handle: Any = None

    def acquire(self) -> "WorkdirLock":
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0, os.SEEK_END)
                if self._handle.tell() == 0:
                    self._handle.write("0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    self._handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, PermissionError) as exc:
            holder = self._read_holder()
            self._handle.close()
            self._handle = None
            raise LockConflict(self.workdir, holder) from exc

        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def _read_holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def __enter__(self) -> "WorkdirLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass
class CheckpointReadResult:
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    applied_count: int = 0
    diagnostics: list[Diagnostic] = field(default_factory=list)
    removed_temp_files: list[str] = field(default_factory=list)


class CheckpointStore:
    def __init__(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        self.path = self.workdir / "checkpoint.json"
        self.temp_prefixes = (
            ".checkpoint.json.",
            "checkpoint.json.",
            ".checkpoint.",
        )

    def cleanup_temporary_files(self) -> list[str]:
        return cleanup_temp_files(self.workdir, self.temp_prefixes)

    def read(
        self,
        *,
        policy_version: str,
        policy_identity: str,
        applied_updates: list[dict[str, str]],
        structure_version: str = CHECKPOINT_STRUCTURE_VERSION,
    ) -> CheckpointReadResult:
        removed = self.cleanup_temporary_files()
        if not self.path.exists():
            return CheckpointReadResult(
                status="fresh",
                removed_temp_files=removed,
            )
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_CORRUPT",
                        message=f"checkpoint cannot be parsed and will be rebuilt: {exc}",
                        source=str(self.path),
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        if not isinstance(envelope, dict) or envelope.get("format") != "authdiag.checkpoint":
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_CORRUPT",
                        message="checkpoint envelope format is invalid; rebuilding",
                        source=str(self.path),
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        observed_structure = envelope.get("structure_version")
        if observed_structure != structure_version:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_STRUCTURE",
                        message="checkpoint structure version changed; rebuilding",
                        source=str(self.path),
                        details={
                            "observed": observed_structure,
                            "expected": structure_version,
                        },
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        payload = envelope.get("payload")
        checksum = envelope.get("checksum")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_CORRUPT",
                        message="checkpoint payload or checksum is missing; rebuilding",
                        source=str(self.path),
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )
        actual_checksum = sha256_bytes(canonical_json_bytes(payload))
        if actual_checksum != checksum:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_CORRUPT",
                        message="checkpoint checksum mismatch; rebuilding",
                        source=str(self.path),
                        details={
                            "expected": checksum,
                            "observed": actual_checksum,
                        },
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        observed_policy_version = payload.get("policy_version")
        if observed_policy_version != policy_version:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_VERSION",
                        message="policy version changed; checkpoint will not be resumed",
                        source=str(self.path),
                        details={
                            "observed": observed_policy_version,
                            "expected": policy_version,
                        },
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        observed_identity = payload.get("policy_identity")
        if observed_identity != policy_identity:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_IDENTITY",
                        message="base policy identity changed; rebuilding checkpoint",
                        source=str(self.path),
                        details={
                            "observed": observed_identity,
                            "expected": policy_identity,
                        },
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        stored_updates = payload.get("applied_updates", [])
        if not isinstance(stored_updates, list):
            stored_updates = []
        if len(stored_updates) > len(applied_updates):
            incompatible = True
        else:
            incompatible = any(
                not isinstance(stored, dict)
                or stored.get("key") != expected.get("key")
                or stored.get("sha256") != expected.get("sha256")
                for stored, expected in zip(stored_updates, applied_updates)
            )
        if incompatible:
            return CheckpointReadResult(
                status="rebuilt",
                diagnostics=[
                    Diagnostic(
                        code="E_CHECKPOINT_UPDATE",
                        message="applied update watermark is incompatible; rebuilding",
                        source=str(self.path),
                        details={
                            "stored_updates": stored_updates,
                            "current_updates": applied_updates,
                        },
                        severity="warning",
                    )
                ],
                removed_temp_files=removed,
            )

        return CheckpointReadResult(
            status="resumed",
            payload=payload,
            applied_count=len(stored_updates),
            removed_temp_files=removed,
        )

    def save(
        self,
        payload: dict[str, Any],
        *,
        structure_version: str = CHECKPOINT_STRUCTURE_VERSION,
    ) -> None:
        payload_with_structure = {
            **payload,
            "structure_version": structure_version,
        }
        envelope = {
            "format": "authdiag.checkpoint",
            "structure_version": structure_version,
            "checksum": sha256_bytes(canonical_json_bytes(payload_with_structure)),
            "payload": payload_with_structure,
        }
        atomic_write_json(self.path, envelope)


def checkpoint_payload(
    *,
    policy_version: str,
    policy_identity: str,
    effective_policy_version: str,
    applied_updates: list[dict[str, str]],
    event_state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "policy_version": policy_version,
        "policy_identity": policy_identity,
        "effective_policy_version": effective_policy_version,
        "applied_updates": applied_updates,
        "event_state": event_state,
        "payload_sha256": sha256_value(
            {
                "policy_version": policy_version,
                "policy_identity": policy_identity,
                "effective_policy_version": effective_policy_version,
                "applied_updates": applied_updates,
                "event_state": event_state,
            }
        ),
    }
