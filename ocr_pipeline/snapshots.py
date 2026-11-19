"""Versioned, checksummed snapshots with atomic whole-snapshot replacement.

Three snapshot categories are supported: ``template_library``,
``recognition_params`` and ``page_results``.  Each artifact carries a monotonic
version and a content checksum.  A commit writes a temp file then atomically
replaces the active snapshot; partial/corrupt files are rejected wholesale.

A non-blocking in-process lock enforces a single effective commit.  A second
concurrent committer immediately receives ``E_STAGE_BUSY`` instead of waiting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import SnapshotCorruptError, StageBusyError
from .util import content_hash, read_json, write_json

SNAPSHOT_CATEGORIES = ("template_library", "recognition_params", "page_results")


def wrap_snapshot(category: str, version: int, payload: dict) -> dict:
    body = {"category": category, "version": version, "payload": payload}
    return {**body, "checksum": content_hash(payload)}


def verify_snapshot(envelope: dict, *, expected_category: str | None = None) -> dict:
    for field in ("category", "version", "payload", "checksum"):
        if field not in envelope:
            raise SnapshotCorruptError(
                f"snapshot missing field {field}",
                {"category": envelope.get("category", "unknown"),
                 "version": envelope.get("version", -1)},
            )
    category = envelope["category"]
    version = envelope["version"]
    if expected_category is not None and category != expected_category:
        raise SnapshotCorruptError(
            "snapshot category mismatch",
            {"category": category, "version": version,
             "expected_category": expected_category},
        )
    actual = content_hash(envelope["payload"])
    if actual != envelope["checksum"]:
        raise SnapshotCorruptError(
            "snapshot checksum mismatch; rejecting partial/corrupt data",
            {"category": category, "version": version,
             "expected": envelope["checksum"], "actual": actual},
        )
    if not isinstance(version, int) or version < 1:
        raise SnapshotCorruptError(
            "snapshot version must be a positive monotonic integer",
            {"category": category, "version": version},
        )
    return envelope


class _NonBlockingLock:
    def __init__(self) -> None:
        self._held = False

    def acquire(self) -> None:
        if self._held:
            raise StageBusyError(
                "a snapshot commit is already in effect; later writer rejected",
                {"lock": "snapshot_commit"},
            )
        self._held = True

    def release(self) -> None:
        self._held = False


@dataclass
class LoadedSnapshot:
    category: str
    version: int
    checksum: str
    payload: dict


class SnapshotStore:
    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)
        self._lock = _NonBlockingLock()

    def path_for(self, category: str) -> str:
        return os.path.join(self.directory, f"{category}.json")

    def commit(self, category: str, version: int, payload: dict) -> dict:
        if category not in SNAPSHOT_CATEGORIES:
            raise SnapshotCorruptError(
                "unknown snapshot category", {"category": category, "version": version}
            )
        self._lock.acquire()
        try:
            existing = None
            if os.path.exists(self.path_for(category)):
                try:
                    existing = self.load(category, verify=False)
                except SnapshotCorruptError:
                    # Corrupt/partial predecessor is replaced wholesale.
                    existing = None
            if existing is not None and version <= existing.version:
                raise SnapshotCorruptError(
                    "snapshot version must be strictly monotonic",
                    {"category": category, "version": version,
                     "existing_version": existing.version},
                )
            envelope = wrap_snapshot(category, version, payload)
            target = self.path_for(category)
            tmp = target + ".tmp"
            write_json(tmp, envelope)
            os.replace(tmp, target)  # atomic whole-snapshot replacement
            return envelope
        finally:
            self._lock.release()

    def load(
        self, category: str, *, verify: bool = True
    ) -> LoadedSnapshot:
        target = self.path_for(category)
        try:
            envelope = read_json(target)
        except Exception as exc:  # unreadable/partial JSON -> corrupt, never partial use
            raise SnapshotCorruptError(
                "snapshot cannot be parsed; refusing partial data",
                {"category": category, "version": -1, "detail": str(exc)},
            ) from exc
        checked = verify_snapshot(envelope, expected_category=category) if verify \
            else envelope
        return LoadedSnapshot(
            category=checked["category"],
            version=int(checked["version"]),
            checksum=checked["checksum"],
            payload=checked["payload"],
        )

    def reload_if_changed(
        self, category: str, previous: LoadedSnapshot | None
    ) -> tuple[LoadedSnapshot, bool]:
        """On restart/reload, replace wholesale when version or checksum changes."""
        current = self.load(category)
        changed = (
            previous is None
            or current.version != previous.version
            or current.checksum != previous.checksum
        )
        return current, changed
