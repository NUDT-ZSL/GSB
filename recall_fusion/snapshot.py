"""Versioned, checksummed snapshots with atomic whole-snapshot replacement."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ErrorCode, PipelineError

SNAPSHOT_KINDS = ("recall_stats", "normalization_params", "fusion_weights")


def compute_checksum(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    kind: str
    version: int
    checksum: str
    payload: dict[str, Any]

    @staticmethod
    def create(kind: str, version: int, payload: dict[str, Any]) -> "Snapshot":
        if kind not in SNAPSHOT_KINDS:
            raise PipelineError(
                ErrorCode.E_VALIDATION,
                "snapshot",
                f"unknown snapshot kind {kind!r}",
                field="kind",
            )
        return Snapshot(
            kind=kind,
            version=version,
            checksum=compute_checksum(payload),
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Snapshot":
        return Snapshot(
            kind=raw["kind"],
            version=int(raw["version"]),
            checksum=str(raw["checksum"]),
            payload=raw["payload"],
        )

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.kind}.json"
        # Whole-file write: readers never observe a partially patched snapshot.
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return path

    @staticmethod
    def load(directory: str | Path, kind: str) -> "Snapshot":
        path = Path(directory) / f"{kind}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            raise PipelineError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "snapshot",
                f"snapshot {kind!r} is unreadable: {exc}",
                field=kind,
                context={"kind": kind, "version": None},
            ) from exc
        snapshot = Snapshot.from_dict(raw)
        expected = compute_checksum(snapshot.payload)
        if expected != snapshot.checksum:
            raise PipelineError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "snapshot",
                (
                    f"checksum mismatch for {kind!r} version {snapshot.version}: "
                    f"expected {expected}, stored {snapshot.checksum}"
                ),
                field=kind,
                context={"kind": kind, "version": snapshot.version},
            )
        return snapshot


class SnapshotStore:
    """In-memory store enforcing monotonic versions and atomic replacement."""

    def __init__(self) -> None:
        self._current: dict[str, Snapshot] = {}
        self.history: list[Snapshot] = []

    def current(self, kind: str) -> Snapshot | None:
        return self._current.get(kind)

    def submit(self, snapshot: Snapshot) -> Snapshot:
        existing = self._current.get(snapshot.kind)
        if existing is not None and snapshot.version < existing.version:
            raise PipelineError(
                ErrorCode.E_SNAPSHOT_ROLLBACK,
                "snapshot",
                (
                    f"cannot replace {snapshot.kind!r} v{existing.version} "
                    f"with older v{snapshot.version}"
                ),
                field=snapshot.kind,
                context={
                    "kind": snapshot.kind,
                    "current_version": existing.version,
                    "offered_version": snapshot.version,
                },
            )
        if compute_checksum(snapshot.payload) != snapshot.checksum:
            raise PipelineError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                "snapshot",
                f"checksum mismatch on submit for {snapshot.kind!r} v{snapshot.version}",
                field=snapshot.kind,
                context={"kind": snapshot.kind, "version": snapshot.version},
            )
        # Whole replacement: the old snapshot object is discarded in one step.
        self._current[snapshot.kind] = snapshot
        self.history.append(snapshot)
        return snapshot

    def load_into(self, directory: str | Path, kinds: list[str] | None = None) -> dict[str, Snapshot]:
        loaded: dict[str, Snapshot] = {}
        for kind in kinds or SNAPSHOT_KINDS:
            path = Path(directory) / f"{kind}.json"
            if not path.exists():
                continue
            snapshot = Snapshot.load(directory, kind)
            self.submit(snapshot)
            loaded[kind] = snapshot
        return loaded
