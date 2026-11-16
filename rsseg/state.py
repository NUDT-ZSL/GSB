from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import E_STATE_INVALID, Failure
from .model import FusionResult, TileResult
from .util import (
    atomic_replace_directory,
    atomic_write_json,
    new_staging_dir,
    read_json,
    remove_tree,
    sha256_bytes,
    sha256_file,
)


@dataclass
class Snapshot:
    path: Path
    manifest: dict[str, Any]
    category: np.ndarray
    tile_results: dict[str, dict[str, np.ndarray]]
    manifest_digest: str

    @property
    def version(self) -> int:
        return int(self.manifest.get("snapshot_version", 0))


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.tile_dir = self.root / "tile_results"
        self.fusion_dir = self.root / "fusion_results"
        self.snapshot_dir = self.root / "snapshot"
        self.backup_dir = self.root / ".snapshot-backup"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tile_dir.mkdir(parents=True, exist_ok=True)
        self.fusion_dir.mkdir(parents=True, exist_ok=True)
        self.recovery_failures: list[Failure] = []
        self._recover()

    def _recover(self) -> None:
        if not self.snapshot_dir.exists() and self.backup_dir.exists():
            try:
                os.replace(self.backup_dir, self.snapshot_dir)
            except OSError:
                pass
        elif self.snapshot_dir.exists() and self.backup_dir.exists():
            remove_tree(self.backup_dir)
        for path in self.root.glob(".staging-*"):
            remove_tree(path)
        for path in self.root.glob(".snapshot-staging-*"):
            remove_tree(path)

    def load_snapshot(self) -> Snapshot | None:
        manifest_path = self.snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            manifest = read_json(manifest_path)
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ValueError("unsupported manifest")
            checksums = manifest.get("checksums", {})
            for name, expected in checksums.items():
                path = self.snapshot_dir / name
                if not path.exists() or sha256_file(path) != expected:
                    raise ValueError(f"snapshot checksum mismatch: {name}")
            category = np.load(self.snapshot_dir / "category.npy", allow_pickle=False)
            tile_results: dict[str, dict[str, np.ndarray]] = {}
            tile_path = self.snapshot_dir / "tile_results.npz"
            if tile_path.exists():
                with np.load(tile_path, allow_pickle=False) as payload:
                    for key in payload.files:
                        tile_results[key] = np.asarray(payload[key])
            return Snapshot(
                path=self.snapshot_dir,
                manifest=manifest,
                category=category,
                tile_results=tile_results,
                manifest_digest=sha256_file(manifest_path),
            )
        except Exception as exc:
            self.recovery_failures.append(
                Failure(E_STATE_INVALID, "recovery", f"ignored incomplete snapshot: {exc}")
            )
            return None

    def _tile_path(self, row: int, col: int, cache_key: str) -> Path:
        return self.tile_dir / f"r{row}_c{col}" / f"{cache_key}.npz"

    def tile_cache_exists(self, row: int, col: int, cache_key: str) -> bool:
        return self._tile_path(row, col, cache_key).is_file()

    def write_tile_result(self, result: TileResult) -> None:
        path = self._tile_path(result.row, result.col, result.cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tile-", suffix=".npz", dir=str(path.parent))
        os.close(fd)
        try:
            np.savez_compressed(
                tmp_name,
                classes=result.classes.astype(np.uint8),
                scores=result.scores.astype(np.float64),
                cache_key=np.asarray(result.cache_key),
                row=np.asarray(result.row, dtype=np.int64),
                col=np.asarray(result.col, dtype=np.int64),
                band_digest=np.asarray(result.band_digest),
                window_count=np.asarray(result.window_count, dtype=np.int64),
            )
            with open(tmp_name, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def load_tile_result(self, row: int, col: int, cache_key: str) -> TileResult | None:
        path = self._tile_path(row, col, cache_key)
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as payload:
                stored_key = str(payload["cache_key"].item())
                if stored_key != cache_key:
                    return None
                if int(payload["row"].item()) != row or int(payload["col"].item()) != col:
                    return None
                classes = np.asarray(payload["classes"], dtype=np.uint8)
                scores = np.asarray(payload["scores"], dtype=np.float64)
                if classes.ndim != 2 or scores.ndim != 3 or scores.shape[1:] != classes.shape:
                    return None
                return TileResult(
                    row=row,
                    col=col,
                    cache_key=cache_key,
                    classes=classes,
                    scores=scores,
                    band_digest=str(payload["band_digest"].item()),
                    window_count=int(payload["window_count"].item()),
                )
        except Exception:
            return None

    def _fusion_path(self, row: int, col: int, dep: str) -> Path:
        return self.fusion_dir / f"r{row}_c{col}" / f"{dep}.npz"

    def fusion_cache_exists(self, row: int, col: int, dep: str) -> bool:
        return self._fusion_path(row, col, dep).is_file()

    def write_fusion_result(self, result: FusionResult) -> None:
        path = self._fusion_path(result.row, result.col, result.dep_fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".fusion-", suffix=".npz", dir=str(path.parent))
        os.close(fd)
        try:
            participant_keys = np.asarray([f"{key}:{value}" for key, value in result.participant_keys])
            np.savez_compressed(
                tmp_name,
                classes=result.classes.astype(np.uint8),
                dep_fingerprint=np.asarray(result.dep_fingerprint),
                participant_keys=participant_keys,
            )
            with open(tmp_name, "rb") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def load_fusion_result(self, row: int, col: int, dep: str) -> FusionResult | None:
        path = self._fusion_path(row, col, dep)
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as payload:
                if str(payload["dep_fingerprint"].item()) != dep:
                    return None
                classes = np.asarray(payload["classes"], dtype=np.uint8)
                participants = tuple(
                    tuple(str(v).split(":", 1)) for v in payload["participant_keys"].tolist()
                )
                return FusionResult(
                    row=row,
                    col=col,
                    dep_fingerprint=dep,
                    classes=classes,
                    participant_keys=participants,  # type: ignore[arg-type]
                )
        except Exception:
            return None

    def write_snapshot(
        self,
        manifest: dict[str, Any],
        category: np.ndarray,
        diagnostics: dict[str, Any],
        tile_results: dict[str, TileResult],
    ) -> tuple[Path, str]:
        staging = new_staging_dir(self.root, prefix=".snapshot-staging-")
        category_path = staging / "category.npy"
        np.save(category_path, category.astype(np.uint8), allow_pickle=False)

        tile_payload: dict[str, np.ndarray] = {}
        for key in sorted(tile_results):
            result = tile_results[key]
            tile_payload[f"classes_{key}"] = result.classes.astype(np.uint8)
            tile_payload[f"scores_{key}"] = result.scores.astype(np.float64)
            tile_payload[f"key_{key}"] = np.asarray(result.cache_key)
        tile_results_path = staging / "tile_results.npz"
        np.savez_compressed(tile_results_path, **tile_payload)

        diagnostics_path = staging / "diagnostics.json"
        atomic_write_json(diagnostics_path, diagnostics)

        try:
            from PIL import Image

            palette = np.asarray(
                [
                    [0, 0, 0, 0],
                    [64, 160, 64, 255],
                    [32, 96, 220, 255],
                    [210, 210, 210, 255],
                    [150, 110, 60, 255],
                ],
                dtype=np.uint8,
            )
            rgba = palette[np.clip(category.astype(np.int64), 0, 4)]
            Image.fromarray(rgba, mode="RGBA").save(staging / "category.png")
        except Exception:
            (staging / "category.png").unlink(missing_ok=True)

        checksums = {
            "category.npy": sha256_file(category_path),
            "tile_results.npz": sha256_file(tile_results_path),
            "diagnostics.json": sha256_file(diagnostics_path),
        }
        manifest = dict(manifest)
        manifest["checksums"] = checksums
        manifest_path = staging / "manifest.json"
        atomic_write_json(manifest_path, manifest)
        atomic_replace_directory(staging, self.snapshot_dir, self.backup_dir)
        return self.snapshot_dir, sha256_file(self.snapshot_dir / "manifest.json")
