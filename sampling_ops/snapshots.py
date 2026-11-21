"""Versioned, checksummed whole-snapshot replacement."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .decision import canonical_bytes
from .errors import fail


def content_checksum(content: Any) -> str:
    return hashlib.sha256(canonical_bytes(content)).hexdigest()


class SnapshotStore:
    """Stores whole config/stat snapshots with monotonic versions.

    A content change never merges into the previous snapshot: the complete
    record is replaced atomically.  Version regressions are rejected.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str) -> Path:
        safe_kind = "".join(character for character in kind if character.isalnum() or character in "-_")
        if not safe_kind:
            fail("E_SNAPSHOT_INVALID", "snapshot kind is empty", field="snapshot_kind")
        return self.directory / f"{safe_kind}.snapshot.json"

    def load(self, kind: str) -> dict[str, Any] | None:
        path = self._path(kind)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            fail(
                "E_SNAPSHOT_INVALID",
                "snapshot file cannot be read or parsed",
                field="snapshot",
                details={"kind": kind, "path": str(path), "error": str(error)},
            )
        required = {"kind", "version", "checksum", "content"}
        if not isinstance(record, dict) or not required.issubset(record):
            fail(
                "E_SNAPSHOT_INVALID",
                "snapshot record is missing required fields",
                field="snapshot",
                details={"kind": kind, "path": str(path), "required": sorted(required)},
            )
        expected = content_checksum(record["content"])
        if record["checksum"] != expected:
            fail(
                "E_SNAPSHOT_INVALID",
                "snapshot content checksum does not match",
                field="checksum",
                details={
                    "kind": kind,
                    "expected": expected,
                    "actual": record["checksum"],
                    "version": record["version"],
                },
            )
        if not isinstance(record["version"], int) or record["version"] < 1:
            fail(
                "E_SNAPSHOT_INVALID",
                "snapshot version must be a positive integer",
                field="version",
                details={"kind": kind, "version": record["version"]},
            )
        return record

    def _write(self, kind: str, version: int, content: Mapping[str, Any]) -> dict[str, Any]:
        record = {
            "kind": kind,
            "version": int(version),
            "checksum": content_checksum(content),
            "algorithm": "sha256",
            "content": dict(content),
        }
        path = self._path(kind)
        temporary = path.with_suffix(f".tmp-{os.getpid()}")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return record

    def put(
        self,
        kind: str,
        content: Mapping[str, Any],
        *,
        requested_version: int | None = None,
    ) -> dict[str, Any]:
        previous: dict[str, Any] | None
        recovered = False
        try:
            previous = self.load(kind)
        except Exception as error:
            if getattr(error, "code", None) != "E_SNAPSHOT_INVALID":
                raise
            previous = None
            recovered = True
        checksum = content_checksum(content)
        if previous is not None and previous["checksum"] == checksum:
            if requested_version is None:
                return {
                    **previous,
                    "changed": False,
                    "recovered": recovered,
                }
            try:
                requested = int(requested_version)
            except (TypeError, ValueError, OverflowError):
                fail(
                    "E_SNAPSHOT_INVALID",
                    "requested snapshot version must be an integer",
                    field="version",
                    details={"kind": kind, "requested_version": requested_version},
                )
            if requested <= previous["version"]:
                return {
                    **previous,
                    "changed": False,
                    "recovered": recovered,
                }
            record = self._write(kind, requested, content)
            return {**record, "changed": True, "recovered": recovered}

        if requested_version is not None:
            try:
                requested = int(requested_version)
            except (TypeError, ValueError, OverflowError):
                fail(
                    "E_SNAPSHOT_INVALID",
                    "requested snapshot version must be an integer",
                    field="version",
                    details={"kind": kind, "requested_version": requested_version},
                )
            if previous is not None and requested <= previous["version"]:
                fail(
                    "E_SNAPSHOT_VERSION_REGRESSION",
                    "changed snapshot content requires a version greater than the previous version",
                    field="version",
                    details={
                        "kind": kind,
                        "requested_version": requested,
                        "previous_version": previous["version"],
                    },
                )
        else:
            requested = (previous["version"] + 1) if previous is not None else 1
        record = self._write(kind, int(requested), content)
        return {**record, "changed": True, "recovered": recovered}

    @staticmethod
    def metadata(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "kind": record["kind"],
            "version": int(record["version"]),
            "checksum": record["checksum"],
            "algorithm": record.get("algorithm", "sha256"),
        }

