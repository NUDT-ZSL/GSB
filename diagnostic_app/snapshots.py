from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .clock import LogicalClock
from .errors import AppError, ErrorCode, ErrorLedger
from .hashing import canonical_json, sha256_bytes


SNAPSHOT_KINDS = ("fusion", "plan", "ready_blocks")


class SnapshotStore:
    def __init__(self, root: str | Path, clock: LogicalClock | None = None,
                 error_ledger: ErrorLedger | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.objects_dir = self.root / "objects"
        self.objects_dir.mkdir(exist_ok=True)
        self.ledger_path = self.root / "snapshot_ledger.jsonl"
        self.clock = clock or LogicalClock()
        self.error_ledger = error_ledger
        self._lock = threading.RLock()
        self._memory: dict[str, dict[str, Any]] = {}

    def latest_path(self, kind: str) -> Path:
        return self.root / f"{kind}.latest.json"

    def _append_ledger(self, event: dict[str, Any]) -> None:
        event = {"logical_ms": self.clock.next_ms(), **event}
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def envelope_checksum(envelope: dict[str, Any]) -> str:
        return sha256_bytes(canonical_json(envelope["payload"]))

    def _write_atomic(self, path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def verify_envelope(self, envelope: dict[str, Any]) -> None:
        expected = self.envelope_checksum(envelope)
        if envelope.get("checksum") != expected:
            error = AppError(
                ErrorCode.SNAPSHOT_CHECKSUM,
                "snapshot payload checksum does not match envelope",
                {"kind": envelope.get("kind"), "version": envelope.get("version"),
                 "expected": expected, "actual": envelope.get("checksum")},
            )
            if self.error_ledger:
                self.error_ledger.record(error)
            raise error

    def load(self, kind: str) -> dict[str, Any] | None:
        with self._lock:
            path = self.latest_path(kind)
            if not path.exists():
                self._memory.pop(kind, None)
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            self.verify_envelope(envelope)
            self._memory[kind] = envelope
            return envelope

    def load_all(self) -> dict[str, dict[str, Any] | None]:
        return {kind: self.load(kind) for kind in SNAPSHOT_KINDS}

    def current_version(self, kind: str) -> int:
        envelope = self.load(kind)
        return int(envelope["version"]) if envelope else 0

    def commit(self, kind: str, payload: dict[str, Any],
               source: str = "service") -> dict[str, Any]:
        if kind not in SNAPSHOT_KINDS:
            raise AppError(ErrorCode.INVALID_REQUEST, f"unknown snapshot kind: {kind}")
        with self._lock:
            current = self.load(kind)
            next_version = int(current["version"]) + 1 if current else 1
            envelope = {
                "kind": kind,
                "version": next_version,
                "logical_ms": self.clock.next_ms(),
                "source": source,
                "checksum_algorithm": "sha256-canonical-json",
                "payload": payload,
            }
            envelope["checksum"] = self.envelope_checksum(envelope)
            object_path = self.objects_dir / f"{kind}-v{next_version:06d}-{envelope['checksum'][:12]}.json"
            encoded = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            object_path.write_bytes(encoded)
            self._write_atomic(self.latest_path(kind), encoded)
            self._memory[kind] = envelope
            self._append_ledger({
                "event": "COMMIT",
                "kind": kind,
                "version": next_version,
                "checksum": envelope["checksum"],
                "object_path": str(object_path.relative_to(self.root)),
                "source": source,
                "whole_replace": True,
            })
            return envelope

    def commit_explicit_version(self, kind: str, version: int, payload: dict[str, Any],
                                source: str = "rollback_attempt") -> dict[str, Any]:
        """Commit an externally supplied version, rejecting old versions."""
        with self._lock:
            current = self.load(kind)
            current_version = int(current["version"]) if current else 0
            if version <= current_version:
                error = AppError(
                    ErrorCode.SNAPSHOT_ROLLBACK,
                    "old snapshot version must not overwrite current newer snapshot",
                    {"kind": kind, "attempted_version": version,
                     "current_version": current_version},
                )
                self._append_ledger({
                    "event": "ROLLBACK_REJECTED",
                    "kind": kind,
                    "attempted_version": version,
                    "current_version": current_version,
                })
                if self.error_ledger:
                    self.error_ledger.record(error)
                raise error
            envelope = {
                "kind": kind,
                "version": int(version),
                "logical_ms": self.clock.next_ms(),
                "source": source,
                "checksum_algorithm": "sha256-canonical-json",
                "payload": payload,
            }
            envelope["checksum"] = self.envelope_checksum(envelope)
            encoded = (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
            self._write_atomic(self.latest_path(kind), encoded)
            self._append_ledger({
                "event": "EXPLICIT_VERSION_REPLACE",
                "kind": kind,
                "version": version,
                "checksum": envelope["checksum"],
                "whole_replace": True,
            })
            return envelope

    def reconcile_after_restart(self, previous: dict[str, dict[str, Any] | None] | None = None
                                ) -> dict[str, Any]:
        """Load latest files and report version/checksum wholesale changes."""
        previous = previous or {}
        loaded = self.load_all()
        changes: dict[str, Any] = {}
        for kind, envelope in loaded.items():
            old = previous.get(kind)
            if envelope is None:
                continue
            if old is not None and int(envelope["version"]) < int(old["version"]):
                error = AppError(
                    ErrorCode.SNAPSHOT_ROLLBACK,
                    "restart snapshot is older than previously active version",
                    {"kind": kind, "disk_version": envelope["version"],
                     "previous_version": old["version"]},
                )
                self._append_ledger({
                    "event": "RESTART_ROLLBACK_REJECTED",
                    "kind": kind,
                    "disk_version": envelope["version"],
                    "previous_version": old["version"],
                })
                if self.error_ledger:
                    self.error_ledger.record(error)
                raise error
            changed = old is None or (
                int(envelope["version"]) != int(old["version"])
                or envelope["checksum"] != old["checksum"]
            )
            changes[kind] = {
                "changed": changed,
                "version": envelope["version"],
                "checksum": envelope["checksum"],
                "version_changed": old is None or envelope["version"] != old["version"],
                "checksum_changed": old is None or envelope["checksum"] != old["checksum"],
                "replacement": "whole_snapshot",
            }
            if changed:
                self._append_ledger({
                    "event": "RESTART_WHOLE_REPLACE",
                    "kind": kind,
                    "version": envelope["version"],
                    "checksum": envelope["checksum"],
                    "version_changed": changes[kind]["version_changed"],
                    "checksum_changed": changes[kind]["checksum_changed"],
                })
        return {"snapshots": loaded, "changes": changes}

    def read_ledger(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        return [json.loads(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines() if line]
