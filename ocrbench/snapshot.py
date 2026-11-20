"""Template-library snapshot store with whole-snapshot replacement.

A snapshot is the complete template library for one charset version.  A
version bump always replaces the stored snapshot as a whole; individual
glyphs are never patched.  The store additionally enforces monotonic version
ordering: attempting to persist an older snapshot over a newer one returns
``E_SNAPSHOT_ROLLBACK``.  Damaged files or checksum mismatches raise
``E_CACHE_CORRUPT`` carrying the offending version number.
"""

from __future__ import annotations
import json
import os
import tempfile
from typing import Optional

from .errors import ErrorCode, make_error
from .templates import TemplateLibrary


class TemplateSnapshotStore:
    """File-backed store that always holds exactly one whole snapshot."""

    FILENAME = "template_snapshot.json"

    def __init__(self, root: str) -> None:
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.path = os.path.join(root, self.FILENAME)

    def current_version(self) -> Optional[int]:
        if not os.path.exists(self.path):
            return None
        return int(self._read_raw()["version"])

    def _read_raw(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise make_error(
                ErrorCode.CACHE_CORRUPT,
                "template snapshot file is unreadable",
                stage="template_snapshot",
            ) from exc

    def load(self) -> TemplateLibrary:
        """Load and verify the current whole snapshot."""

        if not os.path.exists(self.path):
            raise make_error(
                ErrorCode.CACHE_CORRUPT,
                "no template snapshot present",
                stage="template_snapshot",
            )
        raw = self._read_raw()
        version = int(raw.get("version", -1))
        try:
            return TemplateLibrary.from_payload(raw)
        except (KeyError, ValueError, TypeError) as exc:
            raise make_error(
                ErrorCode.CACHE_CORRUPT,
                "template snapshot checksum or structure verification failed",
                stage="template_snapshot",
                version=version,
            ) from exc

    def replace(self, library: TemplateLibrary) -> TemplateLibrary:
        """Atomically replace the whole snapshot, blocking rollbacks.

        An unchanged version reuses the existing snapshot and never rewrites
        it.  A lower version than the stored one is rejected outright.
        """

        current = self.current_version()
        if current is not None:
            if library.version < current:
                raise make_error(
                    ErrorCode.SNAPSHOT_ROLLBACK,
                    "refusing to replace a newer template snapshot with an older one",
                    stage="template_snapshot",
                    stored_version=current,
                    attempted_version=library.version,
                )
            if library.version == current:
                existing = self.load()
                if existing.checksum == library.checksum:
                    # Unchanged snapshot: reuse, do not recompute or rewrite.
                    return existing
                # Same version number but different content is corruption.
                raise make_error(
                    ErrorCode.CACHE_CORRUPT,
                    "same version carries a different checksum",
                    stage="template_snapshot",
                    version=library.version,
                )
        self._atomic_write(library.to_payload())
        return library

    def _atomic_write(self, payload: dict) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp-snap-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def corrupt_for_diagnosis(self) -> int:
        """Test helper: truncate the snapshot to simulate on-disk corruption."""

        version = self.current_version() or -1
        with open(self.path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("glyphs"):
            first = next(iter(raw["glyphs"]))
            raw["glyphs"][first][0][0] = (raw["glyphs"][first][0][0] + 1) % 256
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(raw, handle)
        return int(raw.get("version", version))
