"""Durable rollback journal powering atomic switch and crash recovery.

State machine persisted to ``journal/rollback.json``::

    PREPARED  -> temp image written, per-block verification passed
    COMMITTED -> volume image atomically replaced (terminal)

On restart, an open journal is recovered purely from the persistent bitmap
and temp-area marker: PREPARED journals are re-verified and either completed
or aborted; a journal whose manifest version no longer matches is aborted.
"""
from __future__ import annotations

import enum
import json
import os

from .errors import SnapshotError

JOURNAL_DIRNAME = "journal"
JOURNAL_FILENAME = "rollback.json"
TEMP_IMAGE_NAME = "rollback.tmp"
DONE_MARKER_NAME = "rollback.done"


class JournalState(str, enum.Enum):
    PREPARED = "PREPARED"
    COMMITTED = "COMMITTED"


class Journal:
    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        self.dir = os.path.join(store_dir, JOURNAL_DIRNAME)
        self.path = os.path.join(self.dir, JOURNAL_FILENAME)
        self.temp_image = os.path.join(self.dir, TEMP_IMAGE_NAME)
        self.done_marker = os.path.join(self.dir, DONE_MARKER_NAME)
        os.makedirs(self.dir, exist_ok=True)

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def begin(self, *, snapshot_id: str, snapshot_name: str,
              manifest_version: int, content_digest: str,
              path: str, changed_blocks: int, volume_blocks: int,
              used_path: str, peak_memory_estimate: int) -> dict:
        if self.exists():
            raise SnapshotError(
                "another rollback journal is already open",
                location="journal.Journal.begin",
                journal_path=self.path,
            )
        record = {
            "state": JournalState.PREPARED.value,
            "snapshot_id": snapshot_id,
            "snapshot_name": snapshot_name,
            "manifest_version": int(manifest_version),
            "manifest_content_digest": content_digest,
            "path": used_path,
            "changed_blocks": int(changed_blocks),
            "volume_blocks": int(volume_blocks),
            "peak_memory_estimate": int(peak_memory_estimate),
            "temp_image": os.path.abspath(self.temp_image),
        }
        self._write(record)
        return record

    def mark_prepared(self) -> None:
        record = self.read()
        record["state"] = JournalState.PREPARED.value
        self._write(record)

    def mark_committed(self) -> None:
        record = self.read()
        record["state"] = JournalState.COMMITTED.value
        self._write(record)
        with open(self.done_marker, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> dict:
        if not self.exists():
            raise SnapshotError(
                "rollback journal does not exist",
                location="journal.Journal.read",
            )
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def cleanup(self) -> None:
        for target in (self.path, self.temp_image, self.done_marker):
            if os.path.exists(target):
                os.remove(target)

    def _write(self, record: dict) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
