"""Content-addressed binary stage artifacts plus a versioned index."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .errors import ELedgerCorrupt, EStageBusy


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.stage_root = self.root / "stages"
        self.pending_root = self.root / ".pending"
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self.pending_root.mkdir(parents=True, exist_ok=True)

    def new_pending_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="run-", dir=self.pending_root))

    def save_pending(
        self,
        pending_dir: Path,
        key: str,
        stage: str,
        arrays: Mapping[str, np.ndarray],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        path = pending_dir / f"{stage}-{key}.npz"
        with path.open("wb") as handle:
            np.savez_compressed(handle, **arrays)
        checksum = hash_file(path)
        return {
            "key": key,
            "stage": stage,
            "path": str(path),
            "checksum": checksum,
            "size_bytes": path.stat().st_size,
            "metadata": dict(metadata),
        }

    def finalize(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        finalized: list[dict[str, Any]] = []
        for record in records:
            source = Path(record["path"])
            if not source.exists():
                raise ELedgerCorrupt("pending artifact disappeared", {"record": record})
            target = self.root / "stages" / record["key"][:2] / f"{record['key']}.npz"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and hash_file(target) == record["checksum"]:
                source.unlink(missing_ok=True)
            else:
                os.replace(source, target)
            index_record = dict(record)
            index_record["path"] = str(target.relative_to(self.root))
            finalized.append(index_record)
        return finalized

    def load(self, record: dict[str, Any]) -> dict[str, np.ndarray]:
        path = Path(record["path"])
        if not path.is_absolute():
            path = self.root / path
        if not path.exists():
            raise EStageBusy("cache artifact is missing", {"path": str(path)})
        actual_checksum = hash_file(path)
        if actual_checksum != record["checksum"]:
            raise ELedgerCorrupt(
                "cache artifact checksum mismatch",
                {"path": str(path), "declared": record["checksum"], "actual": actual_checksum},
            )
        try:
            with np.load(path, allow_pickle=False) as payload:
                return {name: payload[name].copy() for name in payload.files}
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ELedgerCorrupt("cache artifact cannot be read", {"path": str(path)}) from exc

    def verify_all(self, index: Mapping[str, Mapping[str, Any]]) -> list[str]:
        failures: list[str] = []
        for key, record in index.items():
            path = Path(record["path"])
            if not path.is_absolute():
                path = self.root / path
            if not path.exists() or hash_file(path) != record["checksum"]:
                failures.append(key)
        return failures
