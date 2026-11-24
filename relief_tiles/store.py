"""Versioned, atomic, digest-validated block persistence."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .config import ReliefConfig
from .errors import BusinessError
from .gray import NormalizationStats


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(path, canonical_json(value).encode("utf-8"))


@dataclass(frozen=True)
class BlockRecord:
    version: str
    row: int
    col: int
    x0: int
    y0: int
    width: int
    height: int
    pixel_count: int
    dtype: str
    digest: str

    def identity_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "row": self.row,
            "col": self.col,
            "x0": self.x0,
            "y0": self.y0,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
            "dtype": self.dtype,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.identity_dict()
        payload["digest"] = self.digest
        return payload


@dataclass(frozen=True)
class LoadedBlock:
    record: BlockRecord
    gray: np.ndarray


@dataclass(frozen=True)
class InvalidBlock:
    row: int
    col: int
    reason: str
    detail: str

    def diagnostic(self) -> dict[str, Any]:
        return {
            "code": "E_DIGEST",
            "row": self.row,
            "col": self.col,
            "reason": self.reason,
            "detail": self.detail,
        }


def compute_block_digest(record: BlockRecord, gray: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json(record.identity_dict()).encode("utf-8"))
    digest.update(np.ascontiguousarray(gray).tobytes(order="C"))
    return digest.hexdigest()


class BlockStore:
    def __init__(self, state_dir: str | Path, config: ReliefConfig) -> None:
        self.config = config
        self.version = config.version
        self.state_dir = Path(state_dir)
        self.versions_dir = self.state_dir / "versions"
        self.archive_dir = self.state_dir / "archive"
        self.version_dir = self.versions_dir / self.version
        self.block_dir = self.version_dir / "blocks"
        self.manifest_path = self.version_dir / "manifest.json"
        self.normalization_path = self.version_dir / "normalization.json"
        self.active_path = self.state_dir / "active_version.json"
        self.interrupt_path = self.state_dir / "interrupted.json"
        self._manifest: dict[str, dict[str, Any]] = {}

    @property
    def block_key(self) -> str:
        return self.version

    def initialize(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.versions_dir.mkdir(parents=True, exist_ok=True)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            self._archive_other_versions()
            self._archive_stale_interrupt()
            self.block_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup_temporary_files()
            self._write_active_marker()
            self._load_manifest()
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "failed to initialize state directory",
                {"state_dir": str(self.state_dir), "error": str(exc)},
            ) from exc

    def _archive_other_versions(self) -> None:
        if not self.versions_dir.exists():
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        for candidate in sorted(self.versions_dir.iterdir()):
            if not candidate.is_dir() or candidate.name == self.version:
                continue
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            destination = (
                self.archive_dir
                / f"{candidate.name}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            shutil.move(str(candidate), str(destination))
            _fsync_directory(self.archive_dir)

    def _archive_stale_interrupt(self) -> None:
        if not self.interrupt_path.exists():
            return
        try:
            payload = json.loads(self.interrupt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if payload.get("version") == self.version:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = (
            self.archive_dir
            / f"interrupted-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
        )
        try:
            os.replace(self.interrupt_path, destination)
        except OSError:
            pass

    def _write_active_marker(self) -> None:
        expected = {"schema_version": 1, "version": self.version}
        current: dict[str, Any] | None = None
        if self.active_path.exists():
            try:
                current = json.loads(self.active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = None
        if current != expected:
            _atomic_write_json(self.active_path, expected)

    def _cleanup_temporary_files(self) -> None:
        if not self.block_dir.exists():
            return
        for candidate in self.block_dir.iterdir():
            if candidate.is_file() and candidate.name.startswith("."):
                try:
                    candidate.unlink()
                except OSError:
                    pass

    def _load_manifest(self) -> None:
        if not self.manifest_path.exists():
            self._manifest = {}
            return
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if data.get("version") != self.version:
                raise ValueError("manifest version mismatch")
            blocks = data.get("blocks", {})
            if not isinstance(blocks, dict):
                raise ValueError("manifest blocks must be an object")
            self._manifest = blocks
        except (OSError, ValueError, AttributeError, json.JSONDecodeError):
            corrupt_path = self.manifest_path.with_name(
                f"manifest.corrupt-{uuid.uuid4().hex[:8]}.json"
            )
            try:
                os.replace(self.manifest_path, corrupt_path)
            except OSError:
                pass
            self._manifest = {}

    def _write_manifest(self) -> None:
        _atomic_write_json(
            self.manifest_path,
            {
                "schema_version": 1,
                "version": self.version,
                "blocks": self._manifest,
            },
        )

    def _block_path(self, row: int, col: int) -> Path:
        return self.block_dir / f"r{row:04d}_c{col:04d}.npz"

    def _manifest_key(self, row: int, col: int) -> str:
        return f"{row},{col}"

    def manifest_entry(self, row: int, col: int) -> dict[str, Any] | None:
        return self._manifest.get(self._manifest_key(row, col))

    def load_block(self, row: int, col: int) -> LoadedBlock | InvalidBlock | None:
        expected = self._expected_record(row, col, digest="")
        path = self._block_path(row, col)
        manifest = self.manifest_entry(row, col)
        if not path.exists():
            if manifest is not None:
                return InvalidBlock(
                    row, col, "missing", "manifest declares a block file that is missing"
                )
            return None

        try:
            with np.load(path, allow_pickle=False) as archive:
                if "gray" not in archive or "metadata" not in archive:
                    return InvalidBlock(
                        row, col, "malformed", "block archive is missing required arrays"
                    )
                gray = np.asarray(archive["gray"])
                metadata_bytes = np.asarray(archive["metadata"], dtype=np.uint8).tobytes()
                metadata = json.loads(metadata_bytes.decode("utf-8"))
        except Exception as exc:
            return InvalidBlock(row, col, "unreadable", f"{type(exc).__name__}: {exc}")

        if not isinstance(metadata, dict):
            return InvalidBlock(row, col, "malformed", "metadata is not an object")
        if metadata.get("version") != self.version:
            return InvalidBlock(
                row,
                col,
                "version_mismatch",
                "stored block version does not match the active config version",
            )
        if gray.ndim != 2 or gray.shape != (expected.height, expected.width):
            return InvalidBlock(
                row,
                col,
                "shape_mismatch",
                f"stored shape {gray.shape} != expected {(expected.height, expected.width)}",
            )
        if gray.dtype != np.uint16:
            return InvalidBlock(
                row, col, "dtype_mismatch", f"stored dtype {gray.dtype} != uint16"
            )
        try:
            declared_pixels = int(metadata.get("pixel_count"))
        except (TypeError, ValueError):
            return InvalidBlock(
                row, col, "pixel_count_mismatch", "pixel_count is not an integer"
            )
        actual_pixels = int(gray.size)
        if declared_pixels != actual_pixels:
            return InvalidBlock(
                row,
                col,
                "pixel_count_mismatch",
                f"declared {declared_pixels} pixels but found {actual_pixels}",
            )
        for field_name, expected_value in (
            ("version", self.version),
            ("row", expected.row),
            ("col", expected.col),
            ("x0", expected.x0),
            ("y0", expected.y0),
            ("width", expected.width),
            ("height", expected.height),
            ("dtype", expected.dtype),
        ):
            if metadata.get(field_name) != expected_value:
                return InvalidBlock(
                    row,
                    col,
                    "metadata_mismatch",
                    f"{field_name}={metadata.get(field_name)!r} != {expected_value!r}",
                )

        try:
            record = BlockRecord(
                version=self.version,
                row=row,
                col=col,
                x0=expected.x0,
                y0=expected.y0,
                width=expected.width,
                height=expected.height,
                pixel_count=declared_pixels,
                dtype=expected.dtype,
                digest=str(metadata.get("digest", "")),
            )
        except (TypeError, ValueError) as exc:
            return InvalidBlock(row, col, "malformed", str(exc))
        actual_digest = compute_block_digest(record, gray)
        if record.digest != actual_digest:
            return InvalidBlock(
                row, col, "digest_mismatch", "stored content digest does not match data"
            )

        if manifest is not None:
            if (
                manifest.get("digest") != record.digest
                or manifest.get("pixel_count") != record.pixel_count
                or manifest.get("x0") != record.x0
                or manifest.get("y0") != record.y0
                or manifest.get("width") != record.width
                or manifest.get("height") != record.height
            ):
                return InvalidBlock(
                    row,
                    col,
                    "manifest_mismatch",
                    "manifest entry does not match the stored block",
                )
        return LoadedBlock(record=record, gray=np.ascontiguousarray(gray))

    def _expected_record(
        self, row: int, col: int, digest: str = ""
    ) -> BlockRecord:
        x0, y0, width, height = self.config.block_bounds(row, col)
        return BlockRecord(
            version=self.version,
            row=row,
            col=col,
            x0=x0,
            y0=y0,
            width=width,
            height=height,
            pixel_count=width * height,
            dtype="uint16",
            digest=digest,
        )

    def save_block(self, row: int, col: int, gray: np.ndarray) -> BlockRecord:
        record = self._expected_record(row, col)
        gray = np.ascontiguousarray(gray, dtype=np.uint16)
        if gray.shape != (record.height, record.width):
            raise BusinessError(
                "E_RENDER",
                "computed block shape does not match its grid bounds",
                {"row": row, "col": col, "shape": list(gray.shape)},
            )
        digest = compute_block_digest(record, gray)
        record = BlockRecord(
            version=record.version,
            row=record.row,
            col=record.col,
            x0=record.x0,
            y0=record.y0,
            width=record.width,
            height=record.height,
            pixel_count=record.pixel_count,
            dtype=record.dtype,
            digest=digest,
        )
        metadata = record.to_dict()
        path = self._block_path(row, col)
        temporary = (
            path.parent
            / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        )
        try:
            with temporary.open("wb") as handle:
                buffer = io.BytesIO()
                np.savez(
                    buffer,
                    gray=gray,
                    metadata=np.frombuffer(
                        canonical_json(metadata).encode("utf-8"), dtype=np.uint8
                    ),
                )
                handle.write(buffer.getvalue())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            self._manifest[self._manifest_key(row, col)] = {
                "digest": record.digest,
                "pixel_count": record.pixel_count,
                "x0": record.x0,
                "y0": record.y0,
                "width": record.width,
                "height": record.height,
            }
            self._write_manifest()
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "failed to persist block",
                {"row": row, "col": col, "error": str(exc)},
            ) from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return record

    def load_normalization(self) -> NormalizationStats | None:
        if not self.normalization_path.exists():
            return None
        try:
            data = json.loads(self.normalization_path.read_text(encoding="utf-8"))
            if data.get("version") != self.version:
                return None
            stats = NormalizationStats.from_dict(data)
            identity = {
                "version": self.version,
                "raw_min": stats.raw_min,
                "raw_max": stats.raw_max,
            }
            if data.get("digest") != _sha256_text(canonical_json(identity)):
                return None
            return stats
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def save_normalization(self, stats: NormalizationStats) -> None:
        identity = {
            "version": self.version,
            "raw_min": stats.raw_min,
            "raw_max": stats.raw_max,
        }
        try:
            _atomic_write_json(
                self.normalization_path,
                {**identity, "digest": _sha256_text(canonical_json(identity))},
            )
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "failed to persist normalization statistics",
                {"error": str(exc)},
            ) from exc

    def write_interrupt(self, payload: dict[str, Any]) -> None:
        try:
            _atomic_write_json(
                self.interrupt_path,
                {
                    "schema_version": 1,
                    "version": self.version,
                    **payload,
                },
            )
        except OSError as exc:
            raise BusinessError(
                "E_STATE",
                "failed to write interruption marker",
                {"error": str(exc)},
            ) from exc

    def clear_interrupt(self) -> None:
        try:
            if self.interrupt_path.exists():
                self.interrupt_path.unlink()
        except OSError:
            pass

    def valid_block_indices(self) -> dict[str, BlockRecord]:
        """Scan persisted files and return valid records by manifest-style key."""

        valid: dict[str, BlockRecord] = {}
        for row, col in self.config.iter_block_indices():
            loaded = self.load_block(row, col)
            if isinstance(loaded, LoadedBlock):
                valid[self._manifest_key(row, col)] = loaded.record
        return valid

    def temporary_file_count(self) -> int:
        if not self.block_dir.exists():
            return 0
        return sum(
            1
            for candidate in self.block_dir.iterdir()
            if candidate.is_file() and candidate.name.startswith(".")
        )
