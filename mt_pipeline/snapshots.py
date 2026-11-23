"""Stage 7: versioned snapshot management with full-replacement semantics."""

import json
import os
from pathlib import Path

from .errors import Codes, PipelineError
from .util import checksum_obj, write_json


class SnapshotStore:
    """Each snapshot kind (vocab / filter / mix) carries a monotonically
    increasing version and a content checksum. Any version or checksum
    change triggers a full atomic replacement, never an in-place patch."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, kind):
        return self.dir / f"{kind}.snapshot.json"

    def _read_raw(self, kind):
        path = self._path(kind)
        if not path.exists():
            return None
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PipelineError(Codes.SNAPSHOT_CORRUPT,
                                f"snapshot {kind} is not parseable: {exc}")
        if not isinstance(snap, dict) or "version" not in snap or "checksum" not in snap:
            raise PipelineError(Codes.SNAPSHOT_CORRUPT,
                                f"snapshot {kind} missing version/checksum fields")
        if checksum_obj(snap.get("content")) != snap["checksum"]:
            raise PipelineError(Codes.SNAPSHOT_CORRUPT,
                                f"snapshot {kind} checksum mismatch")
        return snap

    def load(self, kind):
        snap = self._read_raw(kind)
        return None if snap is None else snap["content"]

    def version(self, kind):
        snap = self._read_raw(kind)
        return None if snap is None else snap["version"]

    def save(self, kind, version, content):
        """Returns 'created' | 'replaced' | 'noop'."""
        existing = self._read_raw(kind)
        checksum = checksum_obj(content)
        if existing is not None:
            if existing["version"] > version:
                raise PipelineError(
                    Codes.SNAPSHOT_ROLLBACK,
                    f"snapshot {kind}: existing version {existing['version']} "
                    f"newer than incoming {version}",
                    detail={"kind": kind, "existing": existing["version"],
                            "incoming": version},
                )
            if existing["version"] == version and existing["checksum"] == checksum:
                return "noop"
        snap = {"kind": kind, "version": version, "checksum": checksum,
                "content": content}
        path = self._path(kind)
        tmp = path.with_suffix(".tmp")
        write_json(tmp, snap)
        os.replace(tmp, path)
        return "replaced" if existing is not None else "created"
