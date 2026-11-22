"""Versioned snapshots with whole replacement (requirement 6).

Three snapshot kinds are managed: block plans ("block_plan"), normalisation
parameters ("norm_params") and ready-block lists ("ready_list"). Each
snapshot carries a monotonically increasing version and a SHA-256 content
checksum. Commits replace the old snapshot as a whole (never incremental
patching); a commit whose version is not greater than the stored one is
rejected with E_SNAPSHOT_ROLLBACK, and a stored snapshot whose checksum
does not match its payload fails loading with E_SNAPSHOT_CORRUPT.
"""
from __future__ import annotations

import hashlib
import json
import os

from .errors import BizError, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK

KINDS = ("block_plan", "norm_params", "ready_list")


def _checksum(payload):
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SnapshotStore:
    """File-backed versioned snapshot store, one JSON file per kind/case."""

    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, kind, case_id):
        if kind not in KINDS:
            raise ValueError("unknown snapshot kind: %s" % kind)
        safe = str(case_id).replace("/", "_")
        return os.path.join(self.root, "%s__%s.json" % (kind, safe))

    def load(self, kind, case_id):
        """Load and verify a snapshot; None when absent.

        Raises E_SNAPSHOT_CORRUPT when the file is unreadable or the
        checksum does not match the payload.
        """
        path = self._path(kind, case_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            version = doc["version"]
            checksum = doc["checksum"]
            payload = doc["payload"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BizError(
                E_SNAPSHOT_CORRUPT,
                "snapshot %s/%s unreadable: %s" % (kind, case_id, exc),
                case_id=case_id, stage="snapshot",
                details={"kind": kind, "path": path})
        if _checksum(payload) != checksum:
            raise BizError(
                E_SNAPSHOT_CORRUPT,
                "snapshot %s/%s checksum mismatch" % (kind, case_id),
                case_id=case_id, stage="snapshot",
                details={"kind": kind, "path": path,
                         "stored_checksum": checksum,
                         "computed_checksum": _checksum(payload)})
        return {"version": version, "checksum": checksum, "payload": payload}

    def commit(self, kind, case_id, version, payload):
        """Whole-replacement commit with monotonic version enforcement."""
        current = self.load(kind, case_id)
        if current is not None and version <= current["version"]:
            raise BizError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot %s/%s version %s does not exceed stored %s"
                % (kind, case_id, version, current["version"]),
                case_id=case_id, stage="snapshot",
                details={"kind": kind, "attempted_version": version,
                         "stored_version": current["version"]})
        doc = {"version": version, "checksum": _checksum(payload),
               "payload": payload}
        path = self._path(kind, case_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=True, indent=2)
        os.replace(tmp, path)
        return {"version": version, "checksum": doc["checksum"]}

    def next_version(self, kind, case_id):
        current = self.load(kind, case_id)
        return 1 if current is None else current["version"] + 1

    def query(self, kind, case_id):
        """Snapshot query entry: metadata without the payload body."""
        snap = self.load(kind, case_id)
        if snap is None:
            return {"kind": kind, "case_id": case_id, "exists": False}
        return {"kind": kind, "case_id": case_id, "exists": True,
                "version": snap["version"], "checksum": snap["checksum"]}
