from __future__ import annotations

import base64
import copy
import fcntl
import json
import os
import re
import shutil
import tempfile
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import EngineError
from .geometry import TileSpec, tile_key
from .model import canonical_json, sha256_bytes, sha256_value

SCHEMA_MANIFEST = "glyph-raster-manifest/v1"
SCHEMA_TILE = "glyph-raster-tile/v1"
SCHEMA_REPORT = "glyph-raster-report/v1"
SCHEMA_INTERRUPTED = "glyph-raster-interrupted/v1"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    data = canonical_json(value) + b"\n"
    atomic_write_bytes(path, data)


class StateLock:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.path = state_dir / ".lock"
        self.handle: Any = None

    def __enter__(self) -> "StateLock":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = self.path.open("a+b")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise EngineError(
                "E_STATE",
                f"cannot acquire state lock: {self.path}",
                {"path": str(self.path)},
            ) from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


@dataclass
class TileRecord:
    key: str
    version: str
    content_version: str
    dependency: dict
    dependency_sha256: str
    width: int
    height: int
    pixel_count: int
    pixel_sha256: str
    pixels: np.ndarray


def encode_tile(spec: TileSpec, pixels: np.ndarray, content_version: str) -> dict:
    if pixels.shape != (spec.height, spec.width):
        raise ValueError(
            f"tile {spec.key} shape {pixels.shape} does not match "
            f"{(spec.height, spec.width)}"
        )
    raw = np.asarray(pixels, dtype=np.uint8).tobytes(order="C")
    return {
        "schema": SCHEMA_TILE,
        "key": spec.key,
        "version": spec.version,
        "contentVersion": content_version,
        "dependencySha256": sha256_value(spec.dependency),
        "dependency": spec.dependency,
        "width": spec.width,
        "height": spec.height,
        "pixelCount": len(raw),
        "pixelSha256": sha256_bytes(raw),
        "pixels": base64.b64encode(zlib.compress(raw, 9)).decode("ascii"),
    }


def decode_tile(payload: Any) -> TileRecord:
    if not isinstance(payload, dict):
        raise EngineError("E_DIGEST", "tile payload must be an object")
    if payload.get("schema") != SCHEMA_TILE:
        raise EngineError("E_DIGEST", "unsupported tile schema")
    try:
        key = payload["key"]
        version = payload["version"]
        content_version = payload["contentVersion"]
        dependency = payload["dependency"]
        dependency_sha = payload["dependencySha256"]
        width = payload["width"]
        height = payload["height"]
        pixel_count = payload["pixelCount"]
        pixel_sha = payload["pixelSha256"]
        encoded = payload["pixels"]
    except KeyError as exc:
        raise EngineError("E_DIGEST", f"tile is missing field: {exc}") from exc
    if not all(
        isinstance(value, str)
        for value in (key, version, content_version, dependency_sha, pixel_sha, encoded)
    ):
        raise EngineError("E_DIGEST", "tile metadata has non-string fields")
    if not isinstance(dependency, dict):
        raise EngineError("E_DIGEST", "tile dependency must be an object")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or isinstance(pixel_count, bool)
        or not isinstance(pixel_count, int)
        or pixel_count < 0
    ):
        raise EngineError("E_DIGEST", "tile dimensions or pixel count are invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        raw = zlib.decompress(compressed)
    except (ValueError, zlib.error) as exc:
        raise EngineError("E_DIGEST", "tile pixel payload cannot be decoded") from exc
    if len(raw) != pixel_count or pixel_count != width * height:
        raise EngineError(
            "E_DIGEST",
            "tile declared pixel count does not match its dimensions or payload",
            {
                "declared": pixel_count,
                "decoded": len(raw),
                "width": width,
                "height": height,
            },
        )
    actual_sha = sha256_bytes(raw)
    if actual_sha != pixel_sha:
        raise EngineError(
            "E_DIGEST",
            "tile pixel digest does not match its payload",
            {"declared": pixel_sha, "actual": actual_sha},
        )
    if dependency_sha != sha256_value(dependency):
        raise EngineError("E_DIGEST", "tile dependency digest does not match its record")
    pixels = np.frombuffer(raw, dtype=np.uint8).copy().reshape(height, width)
    return TileRecord(
        key=key,
        version=version,
        content_version=content_version,
        dependency=dependency,
        dependency_sha256=dependency_sha,
        width=width,
        height=height,
        pixel_count=pixel_count,
        pixel_sha256=pixel_sha,
        pixels=pixels,
    )


class TileStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.tiles_dir = state_dir / "tiles"
        self.archive_dir = state_dir / "archive"
        self.manifest_path = state_dir / "manifest.json"
        self.interrupted_path = state_dir / "interrupted.json"
        self.transaction_path = state_dir / "output-transaction.json"

    def ensure(self) -> None:
        self.tiles_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def tile_path(self, key: str) -> Path:
        return self.tiles_dir / f"{key}.json"

    def read_tile(self, key: str) -> TileRecord:
        path = self.tile_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EngineError("E_DIGEST", f"tile is missing: {key}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EngineError("E_DIGEST", f"tile cannot be read: {key}") from exc
        record = decode_tile(payload)
        if record.key != key:
            raise EngineError(
                "E_DIGEST",
                "tile key does not match its filename",
                {"expected": key, "actual": record.key},
            )
        return record

    def read_tile_metadata(self, key: str) -> dict | None:
        path = self.tile_path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _archive_path(self, key: str, version: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", version):
            version_tag = version[:24]
        else:
            version_tag = sha256_bytes(version.encode("utf-8"))[:24]
        stem = f"{key}.{version_tag}"
        candidate = self.archive_dir / f"{stem}.json"
        suffix = 0
        while candidate.exists():
            suffix += 1
            candidate = self.archive_dir / f"{stem}.{suffix}.json"
        return candidate

    def persist_tile(
        self,
        spec: TileSpec,
        pixels: np.ndarray,
        content_version: str,
    ) -> bool:
        """Persist a tile and return True if an older version was archived."""

        self.ensure()
        path = self.tile_path(spec.key)
        archived = False
        if path.exists():
            old_version: str | None = None
            try:
                old_payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(old_payload, dict) and isinstance(
                    old_payload.get("version"), str
                ):
                    old_version = old_payload["version"]
            except (OSError, json.JSONDecodeError):
                old_version = None
            if old_version != spec.version:
                archive_path = self._archive_path(
                    spec.key, old_version or "invalid"
                )
                try:
                    os.replace(path, archive_path)
                    _fsync_directory(self.archive_dir)
                    _fsync_directory(self.tiles_dir)
                    archived = True
                except OSError as exc:
                    raise EngineError(
                        "E_IO",
                        f"cannot archive old tile: {spec.key}",
                        {"path": str(path), "archive": str(archive_path)},
                    ) from exc
        atomic_write_json(path, encode_tile(spec, pixels, content_version))
        return archived

    def load_manifest(self) -> tuple[dict | None, str | None]:
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, None
        except (OSError, json.JSONDecodeError) as exc:
            return None, str(exc)
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA_MANIFEST:
            return None, "manifest schema is invalid"
        if not isinstance(payload.get("contentVersion"), str):
            return None, "manifest contentVersion is invalid"
        if not isinstance(payload.get("tileVersions"), dict):
            return None, "manifest tileVersions is invalid"
        return payload, None

    def write_manifest(
        self,
        content_version: str,
        scene: object,
        specs: list[TileSpec],
    ) -> None:
        self.ensure()
        payload = {
            "schema": SCHEMA_MANIFEST,
            "contentVersion": content_version,
            "width": scene.width,  # type: ignore[attr-defined]
            "height": scene.height,  # type: ignore[attr-defined]
            "supersampling": scene.supersampling,  # type: ignore[attr-defined]
            "tileWidth": scene.tile_width,  # type: ignore[attr-defined]
            "tileHeight": scene.tile_height,  # type: ignore[attr-defined]
            "tileCount": len(specs),
            "tileVersions": {spec.key: spec.version for spec in specs},
        }
        atomic_write_json(self.manifest_path, payload)

    def write_interrupted(self, payload: dict) -> None:
        self.ensure()
        body = {
            "schema": SCHEMA_INTERRUPTED,
            **copy.deepcopy(payload),
        }
        atomic_write_json(self.interrupted_path, body)

    def clear_interrupted(self) -> None:
        try:
            self.interrupted_path.unlink()
            _fsync_directory(self.state_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EngineError(
                "E_STATE",
                "cannot remove interruption marker",
                {"path": str(self.interrupted_path)},
            ) from exc

    def interrupted_exists(self) -> bool:
        return self.interrupted_path.exists()

    def clean_stale_temps(self) -> None:
        for directory in (self.tiles_dir, self.archive_dir):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                if path.is_file() and path.name.endswith(".tmp"):
                    try:
                        path.unlink()
                    except OSError:
                        pass


@dataclass
class BitmapInfo:
    width: int
    height: int
    content_version: str
    pixel_count: int
    pixel_sha256: str
    header_pixel_sha256: str | None
    header_pixel_count: int | None


@dataclass
class BitmapRecord:
    info: BitmapInfo
    pixels: np.ndarray
    actual_pixel_sha256: str


_VERSION_RE = re.compile(rb"content-version=([0-9a-f]{64})")
_PIXELS_RE = re.compile(rb"pixels=(\d+)")
_SHA_RE = re.compile(rb"sha256=([0-9a-f]{64})")


def encode_pgm(pixels: np.ndarray, content_version: str) -> bytes:
    array = np.asarray(pixels, dtype=np.uint8)
    if array.ndim != 2:
        raise ValueError("PGM pixels must be a 2D array")
    height, width = array.shape
    raw = array.tobytes(order="C")
    pixel_sha = sha256_bytes(raw)
    header = (
        b"P5\n"
        + (
            f"# glyph-raster-v1 content-version={content_version} "
            f"width={width} height={height} pixels={len(raw)} "
            f"sha256={pixel_sha}\n"
        ).encode("ascii")
        + f"{width} {height}\n255\n".encode("ascii")
    )
    return header + raw


def read_pgm(path: Path, *, load_pixels: bool = True) -> BitmapRecord:
    path = Path(path)
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise EngineError(
            "E_IO", f"cannot read bitmap: {path}", {"path": str(path)}
        ) from exc
    with handle:
        tokens: list[bytes] = []
        comment_lines: list[bytes] = []
        data_offset = 0
        while len(tokens) < 4:
            line = handle.readline()
            if not line:
                raise EngineError("E_DIGEST", "bitmap header is truncated")
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(b"#"):
                comment_lines.append(stripped)
                continue
            for token in stripped.split():
                tokens.append(token)
                if len(tokens) == 4:
                    break
            data_offset = handle.tell()
        if tokens[0] != b"P5":
            raise EngineError("E_DIGEST", "bitmap is not binary PGM (P5)")
        try:
            width = int(tokens[1])
            height = int(tokens[2])
            max_value = int(tokens[3])
        except ValueError as exc:
            raise EngineError("E_DIGEST", "bitmap header has invalid dimensions") from exc
        if width <= 0 or height <= 0 or max_value != 255:
            raise EngineError("E_DIGEST", "bitmap header has invalid dimensions")
        expected_count = width * height
        if load_pixels:
            data = handle.read()
        else:
            data = b""
        if load_pixels and len(data) != expected_count:
            raise EngineError(
                "E_DIGEST",
                "bitmap payload size does not match its dimensions",
                {"expected": expected_count, "actual": len(data)},
            )
    combined_comments = b"\n".join(comment_lines)
    version_match = _VERSION_RE.search(combined_comments)
    if version_match is None:
        raise EngineError("E_DIGEST", "bitmap header lacks a content version")
    content_version = version_match.group(1).decode("ascii")
    pixel_match = _PIXELS_RE.search(combined_comments)
    sha_match = _SHA_RE.search(combined_comments)
    declared_count = int(pixel_match.group(1)) if pixel_match else None
    declared_sha = sha_match.group(1).decode("ascii") if sha_match else None
    actual_sha = sha256_bytes(data) if load_pixels else ""
    if declared_count is not None and declared_count != expected_count:
        raise EngineError("E_DIGEST", "bitmap declared pixel count is inconsistent")
    if load_pixels and declared_sha is not None and declared_sha != actual_sha:
        raise EngineError("E_DIGEST", "bitmap pixel digest does not match its payload")
    pixels = (
        np.frombuffer(data, dtype=np.uint8).copy().reshape(height, width)
        if load_pixels
        else np.empty((0, 0), dtype=np.uint8)
    )
    info = BitmapInfo(
        width=width,
        height=height,
        content_version=content_version,
        pixel_count=declared_count if declared_count is not None else expected_count,
        pixel_sha256=declared_sha or "",
        header_pixel_sha256=declared_sha,
        header_pixel_count=declared_count,
    )
    return BitmapRecord(info=info, pixels=pixels, actual_pixel_sha256=actual_sha)


def _copy_with_fsync(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def recover_output_transaction(state_dir: Path) -> str | None:
    transaction_path = state_dir / "output-transaction.json"
    try:
        payload = json.loads(transaction_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(
            "E_STATE",
            "output transaction journal is unreadable",
            {"path": str(transaction_path), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise EngineError("E_STATE", "output transaction journal is invalid")
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            continue
        target_raw = entry.get("target")
        backup_raw = entry.get("backup")
        temporary_raw = entry.get("temporary")
        existed = bool(entry.get("existed"))
        if not isinstance(target_raw, str):
            continue
        target = Path(target_raw)
        backup = Path(backup_raw) if isinstance(backup_raw, str) else None
        temporary = Path(temporary_raw) if isinstance(temporary_raw, str) else None
        try:
            if backup is not None and backup.exists():
                os.replace(backup, target)
                _fsync_directory(target.parent)
            elif not existed:
                target.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise EngineError(
                "E_STATE",
                "cannot roll back an unfinished output transaction",
                {"target": str(target), "error": str(exc)},
            ) from exc
    try:
        transaction_path.unlink()
        _fsync_directory(state_dir)
    except OSError as exc:
        raise EngineError(
            "E_STATE", "cannot clear completed output transaction journal"
        ) from exc
    return "rolled_back"


def _write_prepared_temp(target: Path, data: bytes, transaction_id: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{transaction_id}.tmp"
    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.{transaction_id}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def commit_output_pair(
    state_dir: Path,
    bitmap_path: Path,
    bitmap_data: bytes | None,
    report_path: Path,
    report_data: bytes,
) -> None:
    """Commit bitmap/report with rollback on any failure.

    Passing bitmap_data=None keeps the existing bitmap (used when its content
    version and dimensions already match).
    """

    state_dir.mkdir(parents=True, exist_ok=True)
    transaction_id = uuid.uuid4().hex
    entries: list[dict] = []
    journal_path = state_dir / "output-transaction.json"
    try:
        report_temp = _write_prepared_temp(report_path, report_data, transaction_id)
        report_existed = report_path.exists()
        report_backup: Path | None = None
        if report_existed:
            report_backup = report_path.parent / (
                f".{report_path.name}.{transaction_id}.bak"
            )
            _copy_with_fsync(report_path, report_backup)
        entries.append(
            {
                "target": str(report_path),
                "temporary": str(report_temp),
                "backup": str(report_backup) if report_backup else None,
                "existed": report_existed,
            }
        )

        if bitmap_data is not None:
            bitmap_temp = _write_prepared_temp(
                bitmap_path, bitmap_data, transaction_id
            )
            bitmap_existed = bitmap_path.exists()
            bitmap_backup: Path | None = None
            if bitmap_existed:
                bitmap_backup = bitmap_path.parent / (
                    f".{bitmap_path.name}.{transaction_id}.bak"
                )
                _copy_with_fsync(bitmap_path, bitmap_backup)
            entries.append(
                {
                    "target": str(bitmap_path),
                    "temporary": str(bitmap_temp),
                    "backup": str(bitmap_backup) if bitmap_backup else None,
                    "existed": bitmap_existed,
                }
            )

        atomic_write_json(
            journal_path,
            {
                "schema": "glyph-raster-output-transaction/v1",
                "id": transaction_id,
                "entries": entries,
            },
        )

        for entry in reversed(entries):
            os.replace(entry["temporary"], entry["target"])
        _fsync_directory(bitmap_path.parent)
        if report_path.parent != bitmap_path.parent:
            _fsync_directory(report_path.parent)
            _fsync_directory(bitmap_path.parent)
        for entry in entries:
            backup = entry.get("backup")
            if isinstance(backup, str):
                Path(backup).unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        _fsync_directory(state_dir)
    except Exception as exc:
        try:
            recover_output_transaction(state_dir)
        except Exception:
            pass
        if isinstance(exc, EngineError):
            raise
        raise EngineError(
            "E_IO",
            "failed to commit bitmap/report transaction",
            {"error": str(exc)},
        ) from exc
