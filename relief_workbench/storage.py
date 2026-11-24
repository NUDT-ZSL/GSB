from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import WorkbenchConfig
from .errors import WorkbenchError
from .renderer import GlobalStats, TileSpec

_TILE_MAGIC = b"RLFTIL01"
_HEADER_LEN = struct.Struct("<I")
_SCHEMA_VERSION = 1


class TileInvalid(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


@dataclass(frozen=True)
class TileCacheRecord:
    spec: TileSpec
    version: str
    content_sha256: str
    pixels: np.ndarray


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class StateStore:
    def __init__(self, state_dir: str | Path, config: WorkbenchConfig) -> None:
        self.state_dir = Path(state_dir)
        self.config = config
        self.tiles_dir = self.state_dir / "tiles"
        self.manifest_path = self.state_dir / "manifest.json"
        self.interrupt_path = self.state_dir / "interrupted.json"
        self._manifest: dict[str, Any] | None = None
        self.previous_interruption: dict[str, Any] | None = None
        self.purged = False
        self.purge_reason: str | None = None

    @property
    def version(self) -> str:
        return self.config.version

    def prepare(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        manifest = self._read_manifest_raw()
        has_state_files = (
            self.manifest_path.exists()
            or self.tiles_dir.exists()
            or self.interrupt_path.exists()
        )
        version_matches = (
            manifest is not None and manifest.get("version") == self.version
        )
        if has_state_files and not version_matches:
            self.purged = True
            self.purge_reason = (
                "version_mismatch"
                if manifest is not None
                else "manifest_missing_or_invalid"
            )
            self.purge()
        elif manifest is None and self.tiles_dir.exists():
            self.purged = True
            self.purge_reason = "manifest_missing"
            self.purge()
        else:
            self._manifest = manifest

        self.tiles_dir.mkdir(parents=True, exist_ok=True)
        self._remove_temp_files()
        interruption = self._read_interrupt_raw()
        if interruption is not None and interruption.get("version") == self.version:
            self.previous_interruption = interruption
        self.interrupt_path.unlink(missing_ok=True)
        if self._manifest is None or self._manifest.get("version") != self.version:
            self._manifest = {
                "schema_version": _SCHEMA_VERSION,
                "version": self.version,
                "global_stats": None,
            }
            self._write_manifest()
        else:
            self._write_manifest()

    def _read_manifest_raw(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return None
        return raw

    def _read_interrupt_raw(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.interrupt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _remove_temp_files(self) -> None:
        if not self.state_dir.exists():
            return
        for path in self.state_dir.rglob("*.tmp"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_manifest(self) -> None:
        assert self._manifest is not None
        payload = json.dumps(
            self._manifest,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        _atomic_write(self.manifest_path, payload)

    def purge(self) -> None:
        if self.tiles_dir.exists():
            for path in self.tiles_dir.iterdir():
                if path.is_file() or path.is_symlink():
                    try:
                        path.unlink()
                    except OSError:
                        pass
        for path in (
            self.manifest_path,
            self.interrupt_path,
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._remove_temp_files()
        self._manifest = None

    def load_stats(self) -> GlobalStats | None:
        if self._manifest is None:
            return None
        stats = GlobalStats.from_dict(self._manifest.get("global_stats"))
        if stats is None or stats.pixel_count != self.config.width * self.config.height:
            return None
        return stats

    def save_stats(self, stats: GlobalStats) -> None:
        if self._manifest is None:
            self._manifest = {
                "schema_version": _SCHEMA_VERSION,
                "version": self.version,
                "global_stats": None,
            }
        self._manifest["global_stats"] = stats.to_dict()
        self._write_manifest()

    def tile_path(self, spec: TileSpec) -> Path:
        return self.tiles_dir / f"tile_{spec.name}.tile"

    def write_tile(self, spec: TileSpec, pixels: np.ndarray) -> TileCacheRecord:
        array = np.asarray(pixels)
        if array.dtype != np.uint8 or array.shape != (spec.height, spec.width):
            raise WorkbenchError(
                "E_TILE_SHAPE",
                "tile pixel array does not match its coverage",
                {
                    "tile": spec.to_dict(),
                    "shape": list(array.shape),
                    "dtype": str(array.dtype),
                },
            )
        payload = array.tobytes(order="C")
        digest = hashlib.sha256(payload).hexdigest()
        header = {
            "schema_version": _SCHEMA_VERSION,
            "version": self.version,
            "row": spec.row,
            "col": spec.col,
            "origin_x": spec.origin_x,
            "origin_y": spec.origin_y,
            "width": spec.width,
            "height": spec.height,
            "pixel_count": spec.pixel_count,
            "dtype": "uint8",
            "content_sha256": digest,
        }
        header_bytes = json.dumps(
            header, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        file_bytes = _TILE_MAGIC + _HEADER_LEN.pack(len(header_bytes)) + header_bytes + payload
        _atomic_write(self.tile_path(spec), file_bytes)
        return TileCacheRecord(
            spec=spec,
            version=self.version,
            content_sha256=digest,
            pixels=array.copy(),
        )

    def read_tile(self, spec: TileSpec) -> TileCacheRecord:
        path = self.tile_path(spec)
        try:
            file_bytes = path.read_bytes()
        except FileNotFoundError as exc:
            raise TileInvalid("missing", path=str(path)) from exc
        except OSError as exc:
            raise TileInvalid("unreadable", path=str(path), reason=str(exc)) from exc

        minimum_size = len(_TILE_MAGIC) + _HEADER_LEN.size
        if len(file_bytes) < minimum_size:
            raise TileInvalid("truncated", path=str(path), size=len(file_bytes))
        if file_bytes[: len(_TILE_MAGIC)] != _TILE_MAGIC:
            raise TileInvalid("bad_magic", path=str(path))
        header_length = _HEADER_LEN.unpack(
            file_bytes[len(_TILE_MAGIC) : minimum_size]
        )[0]
        if header_length <= 0 or header_length > 1024 * 1024:
            raise TileInvalid("bad_header_length", path=str(path))
        header_end = minimum_size + header_length
        if header_end > len(file_bytes):
            raise TileInvalid("truncated_header", path=str(path))
        try:
            header = json.loads(file_bytes[minimum_size:header_end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TileInvalid("bad_header", path=str(path)) from exc
        if not isinstance(header, dict):
            raise TileInvalid("bad_header", path=str(path))
        if header.get("schema_version") != _SCHEMA_VERSION:
            raise TileInvalid("schema_mismatch", path=str(path))
        if header.get("version") != self.version:
            raise TileInvalid(
                "version_mismatch",
                path=str(path),
                expected=self.version,
                actual=header.get("version"),
            )
        expected_meta = {
            "row": spec.row,
            "col": spec.col,
            "origin_x": spec.origin_x,
            "origin_y": spec.origin_y,
            "width": spec.width,
            "height": spec.height,
            "pixel_count": spec.pixel_count,
            "dtype": "uint8",
        }
        for key, expected in expected_meta.items():
            if header.get(key) != expected:
                raise TileInvalid(
                    "metadata_mismatch",
                    path=str(path),
                    field=key,
                    expected=expected,
                    actual=header.get(key),
                )
        payload = file_bytes[header_end:]
        if len(payload) != spec.pixel_count:
            raise TileInvalid(
                "pixel_count_mismatch",
                path=str(path),
                declared=spec.pixel_count,
                actual=len(payload),
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != header.get("content_sha256"):
            raise TileInvalid(
                "digest_mismatch",
                path=str(path),
                expected=header.get("content_sha256"),
                actual=digest,
            )
        array = np.frombuffer(payload, dtype=np.uint8).reshape(
            spec.height, spec.width
        ).copy()
        return TileCacheRecord(
            spec=spec,
            version=self.version,
            content_sha256=digest,
            pixels=array,
        )

    def delete_tile(self, spec: TileSpec) -> None:
        try:
            self.tile_path(spec).unlink(missing_ok=True)
        except OSError:
            pass

    def verify_all(self, specs: Iterable[TileSpec]) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for spec in specs:
            try:
                self.read_tile(spec)
            except TileInvalid as exc:
                failures.append(
                    {
                        **spec.to_dict(),
                        "reason": exc.reason,
                        **exc.details,
                    }
                )
        return failures

    def write_interrupt_marker(
        self,
        signal_number: int,
        exit_code: int,
        completed: Iterable[TileSpec],
        remaining: Iterable[TileSpec],
    ) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "version": self.version,
            "signal": signal_number,
            "exit_code": exit_code,
            "completed": [
                {"row": spec.row, "col": spec.col} for spec in completed
            ],
            "remaining": [
                {"row": spec.row, "col": spec.col} for spec in remaining
            ],
        }
        _atomic_write(
            self.interrupt_path,
            json.dumps(payload, sort_keys=True, indent=2).encode("utf-8"),
        )

    def cached_tile_names(self) -> list[str]:
        if not self.tiles_dir.exists():
            return []
        return sorted(path.name for path in self.tiles_dir.glob("tile_*.tile"))
