"""Versioned snapshot store with wholesale replacement.

Three snapshot types share this store: "artifacts" (sampling results),
"ledger" (budget ledger) and "decisions" (per-request decision records).

Rules:
  * each snapshot carries a monotonically increasing version and a
    SHA-256 content checksum;
  * any version or checksum change replaces the snapshot wholesale
    (atomic tmp-write + rename), never incremental patching;
  * an older version may never overwrite a newer one: E_SNAPSHOT_ROLLBACK;
  * a corrupted / checksum-mismatching file: E_SNAPSHOT_CORRUPT, naming
    the snapshot type and version.
"""

from __future__ import annotations

import hashlib
import json
import os

from .errors import ErrorCode, WorkbenchError

SNAPSHOT_TYPES = ("artifacts", "ledger", "decisions")
STAGE_SNAPSHOT = "snapshot"


def _canonical(payload) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def checksum_of(payload) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


class SnapshotStore:
    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, stype: str) -> str:
        if stype not in SNAPSHOT_TYPES:
            raise ValueError(f"unknown snapshot type {stype!r}")
        return os.path.join(self.dir, f"snapshot_{stype}.json")

    def _read_file(self, stype: str) -> dict | None:
        path = self._path(stype)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            raise WorkbenchError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                f"snapshot {stype} is unreadable: {exc}",
                stage=STAGE_SNAPSHOT, snapshot_type=stype, version=None,
            )

    def current_version(self, stype: str) -> int:
        rec = self._read_file(stype)
        return int(rec["version"]) if rec else 0

    def put(self, stype: str, version: int, payload) -> dict:
        """Wholesale replace. Rejects rollback to an older version."""
        existing = self._read_file(stype)
        if existing is not None and version < int(existing["version"]):
            raise WorkbenchError(
                ErrorCode.E_SNAPSHOT_ROLLBACK,
                f"snapshot {stype}: version {version} would roll back "
                f"current version {existing['version']}",
                stage=STAGE_SNAPSHOT, snapshot_type=stype, version=version,
                current_version=existing["version"],
            )
        rec = {
            "type": stype,
            "version": int(version),
            "checksum": checksum_of(payload),
            "payload": payload,
        }
        path = self._path(stype)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        os.replace(tmp, path)  # atomic wholesale replacement
        return {"type": stype, "version": rec["version"], "checksum": rec["checksum"]}

    def put_next(self, stype: str, payload) -> dict:
        return self.put(stype, self.current_version(stype) + 1, payload)

    def get(self, stype: str) -> dict | None:
        """Read + verify checksum. Corruption -> E_SNAPSHOT_CORRUPT."""
        rec = self._read_file(stype)
        if rec is None:
            return None
        version = rec.get("version")
        actual = checksum_of(rec.get("payload"))
        if actual != rec.get("checksum"):
            raise WorkbenchError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                f"snapshot {stype} version {version}: checksum mismatch",
                stage=STAGE_SNAPSHOT, snapshot_type=stype, version=version,
            )
        return rec
