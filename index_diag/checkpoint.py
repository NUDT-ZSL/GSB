"""Four-phase checkpoint persistence with monotonic versions and checksums."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import (
    DiagnosticError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_INCOMPLETE,
)
from .locking import NonBlockingFileLock
from .util import (
    atomic_write_json,
    read_json,
    sha256_json,
)


PHASES = ("analyze", "postings", "compress", "ledger")
PHASE_INDEX = {phase: index for index, phase in enumerate(PHASES)}


def _normalize_batch_list(values: Iterable[int]) -> list[int]:
    result = sorted(set(values))
    for value in result:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "checkpoint batch number is invalid",
                {"batch_no": value},
            )
    return result


@dataclass
class Checkpoint:
    version: int
    phase: str
    completed_by_phase: dict[str, list[int]]
    expected_batches: list[int]
    payload: dict[str, Any]
    content_checksum: str
    envelope_checksum: str = ""

    def __post_init__(self) -> None:
        self.completed_by_phase = {
            phase: _normalize_batch_list(self.completed_by_phase.get(phase, []))
            for phase in PHASES
        }
        self.expected_batches = _normalize_batch_list(self.expected_batches)

    @property
    def cycle_completed(self) -> list[int]:
        if not self.completed_by_phase:
            return []
        return sorted(
            set.intersection(
                *(set(self.completed_by_phase[phase]) for phase in PHASES)
            )
        )

    def body(self) -> dict[str, Any]:
        return {
            "checkpoint_version": self.version,
            "completed_by_phase": {
                phase: list(self.completed_by_phase[phase]) for phase in PHASES
            },
            "content_checksum": self.content_checksum,
            "expected_batches": list(self.expected_batches),
            "format": "index-diag-checkpoint",
            "format_version": 1,
            "payload": self.payload,
            "phase": self.phase,
        }

    def to_envelope(self) -> dict[str, Any]:
        body = self.body()
        body["checksum"] = sha256_json(body)
        return body

    @classmethod
    def create(
        cls,
        *,
        version: int,
        phase: str,
        completed_by_phase: Mapping[str, Iterable[int]],
        expected_batches: Iterable[int],
        payload: dict[str, Any],
    ) -> "Checkpoint":
        return cls(
            version=version,
            phase=phase,
            completed_by_phase={
                phase_name: list(completed_by_phase.get(phase_name, []))
                for phase_name in PHASES
            },
            expected_batches=list(expected_batches),
            payload=payload,
            content_checksum=sha256_json(payload),
        )

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, Any]) -> "Checkpoint":
        try:
            body = dict(envelope)
            actual_envelope_checksum = sha256_json(
                {key: value for key, value in body.items() if key != "checksum"}
            )
            if body.get("checksum") != actual_envelope_checksum:
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "checkpoint envelope checksum mismatch",
                )
            if body.get("format") != "index-diag-checkpoint":
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "checkpoint format marker is invalid",
                )
            version = int(body["checkpoint_version"])
            if version <= 0:
                raise ValueError("checkpoint version must be positive")
            phase = str(body["phase"])
            if phase not in PHASES:
                raise ValueError("checkpoint phase is invalid")
            payload = body["payload"]
            if not isinstance(payload, dict):
                raise ValueError("checkpoint payload must be an object")
            content_checksum = str(body["content_checksum"])
            if content_checksum != sha256_json(payload):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    "checkpoint payload checksum mismatch",
                )
            completed = body["completed_by_phase"]
            if not isinstance(completed, dict):
                raise ValueError("completed_by_phase must be an object")
            checkpoint = cls(
                version=version,
                phase=phase,
                completed_by_phase={
                    str(key): list(value) for key, value in completed.items()
                },
                expected_batches=list(body["expected_batches"]),
                payload=payload,
                content_checksum=content_checksum,
                envelope_checksum=str(body["checksum"]),
            )
        except DiagnosticError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "checkpoint metadata is invalid",
                {"error": str(exc)},
            ) from exc
        return checkpoint


class CheckpointStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        lock_path: str | os.PathLike[str] | None = None,
    ):
        self.path = Path(path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.path.with_name(self.path.name + ".lock")
        )

    def load(self) -> Checkpoint | None:
        try:
            envelope = read_json(self.path)
        except FileNotFoundError:
            return None
        except DiagnosticError:
            raise
        if not isinstance(envelope, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "checkpoint root must be an object",
                {"path": str(self.path)},
            )
        return Checkpoint.from_envelope(envelope)

    def commit(
        self,
        *,
        phase: str,
        completed_by_phase: Mapping[str, Iterable[int]],
        expected_batches: Iterable[int],
        payload: dict[str, Any],
    ) -> Checkpoint:
        if phase not in PHASES:
            raise DiagnosticError(
                E_STAGE_INCOMPLETE,
                "unknown checkpoint phase",
                {"phase": phase},
            )
        expected = _normalize_batch_list(expected_batches)
        normalized_completed = {
            phase_name: _normalize_batch_list(
                completed_by_phase.get(phase_name, [])
            )
            for phase_name in PHASES
        }
        if not set(expected).issubset(set(normalized_completed[phase])):
            raise DiagnosticError(
                E_STAGE_INCOMPLETE,
                "checkpoint commit requires every expected batch in the stage",
                {
                    "phase": phase,
                    "expected_batches": expected,
                    "completed_batches": normalized_completed[phase],
                },
            )
        phase_position = PHASE_INDEX[phase]
        for earlier in PHASES[:phase_position]:
            if not set(expected).issubset(set(normalized_completed[earlier])):
                raise DiagnosticError(
                    E_STAGE_INCOMPLETE,
                    "checkpoint stage dependency is incomplete",
                    {
                        "phase": phase,
                        "missing_phase": earlier,
                        "expected_batches": expected,
                    },
                )
        with NonBlockingFileLock(self.lock_path):
            previous = self.load()
            version = 1 if previous is None else previous.version + 1
            checkpoint = Checkpoint.create(
                version=version,
                phase=phase,
                completed_by_phase=normalized_completed,
                expected_batches=expected,
                payload=payload,
            )
            atomic_write_json(self.path, checkpoint.to_envelope())
            return checkpoint

    def replace_with_explicit_version(self, checkpoint: Checkpoint) -> Checkpoint:
        """Testing and repair hook; rollback attempts are rejected."""

        with NonBlockingFileLock(self.lock_path):
            previous = self.load()
            if previous is not None and checkpoint.version < previous.version:
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    "checkpoint version would roll back",
                    {
                        "actual": checkpoint.version,
                        "current": previous.version,
                    },
                )
            atomic_write_json(self.path, checkpoint.to_envelope())
            return checkpoint
