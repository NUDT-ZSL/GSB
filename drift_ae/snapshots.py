"""Versioned, checksummed, whole-object snapshots with atomic replacement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import SnapshotRollbackError
from .io_utils import atomic_write_json, payload_checksum, read_json


SNAPSHOT_KINDS = ("plan", "threshold", "result")


class SnapshotStore:
    def __init__(self, state_dir: str | Path):
        self.root = Path(state_dir) / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)

    def kind_dir(self, kind: str) -> Path:
        if kind not in SNAPSHOT_KINDS:
            raise ValueError(f"unknown snapshot kind: {kind}")
        path = self.root / kind
        path.mkdir(parents=True, exist_ok=True)
        (path / "versions").mkdir(exist_ok=True)
        return path

    def current_path(self, kind: str) -> Path:
        return self.kind_dir(kind) / "current.json"

    def load(self, kind: str) -> dict[str, Any] | None:
        path = self.current_path(kind)
        if not path.exists():
            return None
        envelope = read_json(path)
        self.verify(envelope)
        return envelope

    def load_payload(self, kind: str, default: Any = None) -> Any:
        envelope = self.load(kind)
        return default if envelope is None else envelope["payload"]

    def commit(self, kind: str, payload: Any, *, expected_version: int | None = None) -> dict[str, Any]:
        current = self.load(kind)
        current_version = int(current["version"]) if current else 0
        if expected_version is not None and expected_version != current_version:
            from .errors import StageBusyError

            raise StageBusyError(
                "snapshot version conflict",
                details={"kind": kind, "expected": expected_version, "current": current_version},
            )
        version = current_version + 1
        envelope = self._make_envelope(kind, version, payload)
        self._write_envelope(envelope)
        return envelope

    def replace_if_changed(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Whole-replace when an incoming version/checksum differs.

        Lower versions are rollback attempts and return ``E_SNAPSHOT_ROLLBACK``.
        """

        self.verify(envelope)
        kind = str(envelope["kind"])
        incoming_version = int(envelope["version"])
        incoming_checksum = str(envelope["checksum"])
        current = self.load(kind)

        if current is not None and incoming_version < int(current["version"]):
            error = SnapshotRollbackError(
                "refusing to replace a newer snapshot with an older version",
                details={
                    "kind": kind,
                    "current_version": current["version"],
                    "incoming_version": incoming_version,
                    "error_code": SnapshotRollbackError.code,
                },
            )
            return error.to_outcome()

        if current is not None:
            same_version = incoming_version == int(current["version"])
            same_checksum = incoming_checksum == current["checksum"]
            if same_version and same_checksum:
                return {
                    "ok": True,
                    "replaced": False,
                    "reason": "identical_snapshot",
                    "kind": kind,
                    "version": incoming_version,
                }
            if incoming_version == int(current["version"]) and not same_checksum:
                # A same-version/checksum change is still installed as one whole
                # object; commit() is the normal path that allocates next version.
                pass

        self._write_envelope(envelope)
        return {
            "ok": True,
            "replaced": True,
            "reason": "version_or_checksum_changed",
            "kind": kind,
            "version": incoming_version,
        }

    def attempt_rollback(self, kind: str, target_version: int | None = None) -> dict[str, Any]:
        """Explicit rollback API. Rollback is intentionally never permitted."""

        current = self.load(kind)
        details = {
            "kind": kind,
            "target_version": target_version,
            "current_version": current["version"] if current else 0,
            "error_code": SnapshotRollbackError.code,
        }
        return SnapshotRollbackError(
            "snapshot rollback is disabled; restore forward from a newer snapshot",
            details=details,
        ).to_outcome()

    def rollback_to_version(self, kind: str, target_version: int | None = None) -> None:
        outcome = self.attempt_rollback(kind, target_version)
        raise SnapshotRollbackError(outcome["error"], details=outcome["details"])

    def verify(self, envelope: dict[str, Any]) -> None:
        from .errors import ChecksumMismatchError

        required = {"kind", "version", "payload", "checksum"}
        if not isinstance(envelope, dict) or not required.issubset(envelope):
            raise ChecksumMismatchError("snapshot envelope is missing required fields")
        expected = self.checksum_for(envelope["kind"], int(envelope["version"]), envelope["payload"])
        if expected != envelope["checksum"]:
            raise ChecksumMismatchError(
                "snapshot checksum mismatch",
                details={"kind": envelope["kind"], "version": envelope["version"]},
            )

    @staticmethod
    def checksum_for(kind: str, version: int, payload: Any) -> str:
        return payload_checksum({"kind": kind, "version": version, "payload": payload})

    def _make_envelope(self, kind: str, version: int, payload: Any) -> dict[str, Any]:
        return {
            "kind": kind,
            "version": version,
            "checksum": self.checksum_for(kind, version, payload),
            "payload": payload,
        }

    def _write_envelope(self, envelope: dict[str, Any]) -> None:
        kind = envelope["kind"]
        version = int(envelope["version"])
        directory = self.kind_dir(kind)
        atomic_write_json(directory / "versions" / f"v{version:08d}.json", envelope)
        atomic_write_json(directory / "current.json", envelope)
