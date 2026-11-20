"""Versioned snapshots for config / ground truth / error statistics.

Each snapshot carries a monotonically increasing version and a SHA-256
checksum of its canonical payload. Any version or checksum change
replaces the whole snapshot file atomically (write temp + rename); no
incremental patching. An older version can never overwrite a newer one:
the attempt returns E_SNAPSHOT_ROLLBACK. A corrupted file or checksum
mismatch on load returns E_SNAPSHOT_CORRUPT naming category and version.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from . import codes
from .errors import BusinessError

CATEGORIES = ("config", "ground_truth", "error_stats")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def checksum_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, category: str) -> Path:
        return self.dir / f"{category}.snapshot.json"

    def load(self, category: str) -> Optional[Dict[str, Any]]:
        path = self._path(category)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            version = data.get("version")
            payload = data.get("payload")
            stored = data.get("checksum")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise BusinessError(
                codes.E_SNAPSHOT_CORRUPT,
                f"snapshot {category} unreadable: {exc}",
                stage="snapshot",
                detail={"category": category, "version": None},
            )
        if checksum_of(payload) != stored:
            raise BusinessError(
                codes.E_SNAPSHOT_CORRUPT,
                f"snapshot {category} v{version} checksum mismatch",
                stage="snapshot",
                detail={"category": category, "version": version},
            )
        return data

    def save(self, category: str, payload: Any,
             version: Optional[int] = None) -> Dict[str, Any]:
        existing = self.load(category)
        if existing is not None:
            old_version = int(existing["version"])
            new_version = old_version + 1 if version is None else int(version)
            if new_version <= old_version:
                raise BusinessError(
                    codes.E_SNAPSHOT_ROLLBACK,
                    f"snapshot {category}: version {new_version} would "
                    f"overwrite newer/equal version {old_version}",
                    stage="snapshot",
                    detail={"category": category, "version": new_version,
                            "existing_version": old_version},
                )
            if checksum_of(existing["payload"]) == checksum_of(payload):
                return existing
        else:
            new_version = 1 if version is None else int(version)
            if new_version < 1:
                raise BusinessError(
                    codes.E_SNAPSHOT_ROLLBACK,
                    f"snapshot {category}: initial version must be >= 1",
                    stage="snapshot",
                    detail={"category": category, "version": new_version},
                )

        record = {
            "category": category,
            "version": new_version,
            "checksum": checksum_of(payload),
            "payload": payload,
        }
        path = self._path(category)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                  indent=2) + "\n", encoding="utf-8",
                       newline="\n")
        os.replace(tmp, path)
        return record
