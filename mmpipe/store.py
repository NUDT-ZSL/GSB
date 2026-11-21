"""Durable per-stage snapshot store with atomic commits and checksums.

On-disk layout for a stage::

    snapshots/<stage>/
        v<version>/
            manifest.json        # version, pair set, checksums, config digest
            COMMITTED            # empty marker; its atomic rename is the commit
            embeddings/<pair>.npy
        .tmp-<version>/          # staging area, never visible as committed

Commit is all-or-nothing: a stage is only marked complete once *every* pair is
materialized and the checksum verified. A version or checksum change triggers a
full replacement of the old snapshot (never an in-place patch).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

from .config import PipelineConfig
from .errors import SnapshotCorruptError


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".write-tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass(frozen=True)
class CommittedSnapshot:
    stage: str
    version: int
    pair_ids: Tuple[str, ...]
    config_digest: str
    embeddings_checksum: str
    path: Path

    def load(self, pair_id: str) -> np.ndarray:
        return np.load(self.path / "embeddings" / f"{pair_id}.npy")

    def load_all(self) -> Dict[str, np.ndarray]:
        return {pair_id: self.load(pair_id) for pair_id in self.pair_ids}


class SnapshotStore:
    def __init__(self, root: Path, config: PipelineConfig) -> None:
        self.root = Path(root)
        self.config = config
        self.snapshots_dir = self.root / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ load
    def _stage_dir(self, stage: str) -> Path:
        return self.snapshots_dir / stage

    def _cleanup_temp(self, stage: str) -> None:
        """Uncommitted staging dirs are always discarded (recovery rule)."""
        stage_dir = self._stage_dir(stage)
        if not stage_dir.exists():
            return
        for child in stage_dir.iterdir():
            if child.is_dir() and child.name.startswith(".tmp-"):
                shutil.rmtree(child, ignore_errors=True)

    def load_committed(
        self, stage: str, *, force_rebuild: bool = False
    ) -> Optional[CommittedSnapshot]:
        """Return the latest valid committed snapshot for ``stage``.

        Raises E_SNAPSHOT_CORRUPT naming the stage and version when a committed
        directory cannot be fully parsed or verified. Partial/parseable data is
        never used. With ``force_rebuild`` the corrupt tree is replaced.
        """
        stage_dir = self._stage_dir(stage)
        self._cleanup_temp(stage)
        if not stage_dir.exists():
            return None
        versions = sorted(
            int(child.name[1:])
            for child in stage_dir.iterdir()
            if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit()
        )
        if not versions:
            return None
        version = versions[-1]
        version_dir = stage_dir / f"v{version}"

        def corrupt(reason: str) -> SnapshotCorruptError:
            return SnapshotCorruptError(
                f"snapshot for stage {stage!r} v{version} is corrupt: {reason}",
                stage=stage,
                version=version,
            )

        try:
            if not (version_dir / "COMMITTED").is_file():
                raise corrupt("missing COMMITTED marker")
            manifest_path = version_dir / "manifest.json"
            if not manifest_path.is_file():
                raise corrupt("missing manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pair_ids = tuple(manifest["pair_ids"])
            embeddings_dir = version_dir / "embeddings"
            if tuple(sorted(pair_ids)) != tuple(pair_ids):
                raise corrupt("pair_ids are not in canonical sorted order")
            for pair_id in pair_ids:
                if not (embeddings_dir / f"{pair_id}.npy").is_file():
                    raise corrupt(f"missing embedding for pair {pair_id}")
            actual_checksum = self._compute_embeddings_checksum(embeddings_dir, pair_ids)
            if actual_checksum != manifest["embeddings_checksum"]:
                raise corrupt(
                    f"checksum mismatch (manifest={manifest['embeddings_checksum']}, "
                    f"actual={actual_checksum})"
                )
            if manifest.get("config_digest") != self._config_digest():
                raise corrupt("config digest mismatch")
            if int(manifest["version"]) != version:
                raise corrupt("manifest version disagrees with directory version")
        except SnapshotCorruptError:
            if force_rebuild:
                shutil.rmtree(version_dir, ignore_errors=True)
                return None
            raise

        return CommittedSnapshot(
            stage=stage,
            version=version,
            pair_ids=pair_ids,
            config_digest=manifest["config_digest"],
            embeddings_checksum=actual_checksum,
            path=version_dir,
        )

    # --------------------------------------------------------------- staging
    def begin_stage(self, stage: str, next_version: int) -> Path:
        """Create a fresh staging area; any prior temp state is discarded."""
        stage_dir = self._stage_dir(stage)
        self._cleanup_temp(stage)
        staging = stage_dir / f".tmp-{next_version}"
        (staging / "embeddings").mkdir(parents=True, exist_ok=True)
        return staging

    def write_embedding(self, staging: Path, pair_id: str, array: np.ndarray) -> None:
        np.save(staging / "embeddings" / f"{pair_id}.npy", np.asarray(array, dtype=np.float32))

    def commit_stage(
        self,
        stage: str,
        staging: Path,
        pair_ids: Sequence[str],
        next_version: int,
        *,
        extra: Optional[dict] = None,
    ) -> CommittedSnapshot:
        """Atomically publish a complete stage and replace older versions."""
        ordered = tuple(sorted(pair_ids))
        embeddings_dir = staging / "embeddings"
        checksum = self._compute_embeddings_checksum(embeddings_dir, ordered)
        manifest = {
            "stage": stage,
            "version": next_version,
            "pair_ids": list(ordered),
            "pair_count": len(ordered),
            "config_digest": self._config_digest(),
            "embeddings_checksum": checksum,
        }
        if extra:
            manifest.update(extra)
        _atomic_write_text(staging / "manifest.json", json.dumps(manifest, sort_keys=True, indent=2))

        stage_dir = self._stage_dir(stage)
        final_dir = stage_dir / f"v{next_version}"
        if final_dir.exists():
            shutil.rmtree(final_dir)

        # Full replacement: remove every older version before publishing the new one.
        for child in list(stage_dir.iterdir()):
            if child.is_dir() and child.name.startswith("v") and child.name != f"v{next_version}":
                shutil.rmtree(child, ignore_errors=True)

        os.replace(staging, final_dir)
        # The COMMITTED marker rename is the single atomic visibility point.
        marker = final_dir / "COMMITTED"
        _atomic_write_text(marker, "")
        return CommittedSnapshot(
            stage=stage,
            version=next_version,
            pair_ids=ordered,
            config_digest=self._config_digest(),
            embeddings_checksum=checksum,
            path=final_dir,
        )

    def next_version(self, stage: str) -> int:
        stage_dir = self._stage_dir(stage)
        if not stage_dir.exists():
            return 1
        versions = [
            int(child.name[1:])
            for child in stage_dir.iterdir()
            if child.is_dir() and child.name.startswith("v") and child.name[1:].isdigit()
        ]
        return (max(versions) + 1) if versions else 1

    # -------------------------------------------------------------- utilities
    def _config_digest(self) -> str:
        return hashlib.sha256(self.config.digest_input()).hexdigest()

    @staticmethod
    def _compute_embeddings_checksum(embeddings_dir: Path, pair_ids: Iterable[str]) -> str:
        digest = hashlib.sha256()
        for pair_id in pair_ids:
            digest.update(pair_id.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(_sha256_file(embeddings_dir / f"{pair_id}.npy").encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

