from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator

from .calibration import CalibrationStats
from .errors import (
    DiagnosticError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .formats import ParamSet
from .utils import sha256_json


@dataclass(frozen=True)
class Snapshot:
    version: int
    params: ParamSet
    calibration: CalibrationStats
    model_fingerprint: str
    params_version: str
    calibration_version: str
    checksum: str

    def layer(self, name: str):
        return self.params.layer(name)

    def payload(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.version,
            "model_fingerprint": self.model_fingerprint,
            "params": self.params.to_dict(),
            "calibration": self.calibration.to_dict(),
            "params_version": self.params_version,
            "calibration_version": self.calibration_version,
        }

    def compute_checksum(self) -> str:
        return "snap-sha256:" + sha256_json(self.payload())

    def verify_checksum(self) -> None:
        if self.compute_checksum() != self.checksum:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum does not match its content",
                field="checksum",
                details={
                    "snapshot_version": self.version,
                    "stored_checksum": self.checksum,
                    "computed_checksum": self.compute_checksum(),
                },
            )

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "checksum": self.checksum}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Snapshot":
        try:
            snapshot = cls(
                version=int(payload["snapshot_version"]),
                params=ParamSet.from_dict(payload["params"]),
                calibration=CalibrationStats.from_dict(payload["calibration"]),
                model_fingerprint=str(payload["model_fingerprint"]),
                params_version=str(payload["params_version"]),
                calibration_version=str(payload["calibration_version"]),
                checksum=str(payload["checksum"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot is malformed: {exc}",
                field="snapshot",
            ) from exc
        snapshot.verify_checksum()
        return snapshot

    @classmethod
    def from_json(cls, text: str) -> "Snapshot":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                f"snapshot JSON is truncated or invalid: {exc.msg}",
                field="snapshot",
            ) from exc
        if not isinstance(payload, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot root must be an object",
                field="snapshot",
            )
        return cls.from_dict(payload)

    @classmethod
    def from_file(cls, path: str | Path) -> "Snapshot":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


def build_snapshot(
    params: ParamSet,
    calibration: CalibrationStats,
    *,
    version: int,
    model_fingerprint: str = "",
) -> Snapshot:
    if int(version) < 1:
        raise ValueError("snapshot version must be >= 1")
    versioned_layers = tuple(
        layer.versions_with_calibration(calibration) for layer in params.layers
    )
    versioned_params = replace(params, layers=versioned_layers)
    params_version = "params-" + sha256_json(
        [layer.to_dict() for layer in versioned_layers]
    )[:16]
    calibration_version = calibration.version
    draft = Snapshot(
        version=int(version),
        params=versioned_params,
        calibration=calibration,
        model_fingerprint=model_fingerprint,
        params_version=params_version,
        calibration_version=calibration_version,
        checksum="",
    )
    return replace(draft, checksum=draft.compute_checksum())


class SnapshotStore:
    """A whole-snapshot store with non-blocking, single-writer staging."""

    def __init__(self, initial: Snapshot | None = None) -> None:
        self._lock = threading.Lock()
        self._stage_lock = threading.Lock()
        self._current = initial
        self._history: list[Snapshot] = []
        if initial is not None:
            initial.verify_checksum()
            self._history.append(initial)

    @property
    def current(self) -> Snapshot | None:
        with self._lock:
            return self._current

    @property
    def history(self) -> tuple[Snapshot, ...]:
        with self._lock:
            return tuple(self._history)

    @contextmanager
    def hold_stage(self) -> Iterator[None]:
        """Hold the single-writer slot to exercise deterministic busy handling."""

        if not self._stage_lock.acquire(blocking=False):
            raise DiagnosticError(
                E_STAGE_BUSY,
                "another snapshot submission is already in progress",
            )
        try:
            yield
        finally:
            self._stage_lock.release()

    def commit(self, candidate: Snapshot) -> Snapshot:
        if not self._stage_lock.acquire(blocking=False):
            raise DiagnosticError(
                E_STAGE_BUSY,
                "another snapshot submission is already in progress",
            )
        try:
            return self._commit_locked(candidate)
        finally:
            self._stage_lock.release()

    def _commit_locked(self, candidate: Snapshot) -> Snapshot:
        candidate.verify_checksum()
        with self._lock:
            current = self._current
            if current is None:
                self._current = candidate
                self._history.append(candidate)
                return candidate
            if candidate.version < current.version:
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"snapshot rollback rejected: candidate v{candidate.version} "
                        f"is older than committed v{current.version}"
                    ),
                    source_version=candidate.version,
                    current_version=current.version,
                )
            if candidate.version == current.version:
                if candidate.checksum == current.checksum:
                    return current
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    (
                        f"snapshot v{candidate.version} conflicts with the committed "
                        "version; equal version numbers cannot replace content"
                    ),
                    source_version=candidate.checksum,
                    current_version=current.checksum,
                )
            # Never patch individual layers in place. The committed reference is
            # atomically replaced by the complete immutable candidate snapshot.
            self._current = candidate
            self._history.append(candidate)
            return candidate
