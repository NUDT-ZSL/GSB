"""Fixed-block binary volume image with strong geometry validation."""
from __future__ import annotations

import json
import os

from .digest import sha256_file, sha256_hex
from .errors import VolumeError

SUPPORTED_BLOCK_SIZES = frozenset({512, 1024, 2048, 4096, 8192, 16384, 32768, 65536})
META_SUFFIX = ".meta.json"


class Volume:
    """A fixed-block-size logical block device stored as one binary image.

    The companion ``<image>.meta.json`` file records block_size, block_count
    and the monotonically increasing ``volume_version`` (incremented once per
    successful snapshot creation).
    """

    def __init__(self, image_path: str, block_size: int, block_count: int):
        self.image_path = os.path.abspath(image_path)
        self._validate_geometry(block_size, block_count)
        self.block_size = int(block_size)
        self.block_count = int(block_count)
        self.meta_path = self.image_path + META_SUFFIX
        self._handle = None
        self._open()
        self._verify_image_length()

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _validate_geometry(block_size: int, block_count: int) -> None:
        if not isinstance(block_size, int) or isinstance(block_size, bool):
            raise VolumeError(
                "block_size must be an integer",
                location="volume._validate_geometry",
                block_size=repr(block_size),
            )
        if block_size not in SUPPORTED_BLOCK_SIZES:
            raise VolumeError(
                "unsupported block size",
                location="volume._validate_geometry",
                block_size=block_size,
                supported=sorted(SUPPORTED_BLOCK_SIZES),
            )
        if not isinstance(block_count, int) or isinstance(block_count, bool):
            raise VolumeError(
                "block_count must be an integer",
                location="volume._validate_geometry",
                block_count=repr(block_count),
            )
        if block_count <= 0:
            raise VolumeError(
                "block_count must be positive",
                location="volume._validate_geometry",
                block_count=block_count,
            )

    def _open(self) -> None:
        if not os.path.exists(self.image_path):
            raise VolumeError(
                "volume image does not exist",
                location="volume._open",
                image_path=self.image_path,
            )
        self._handle = open(self.image_path, "r+b")

    def _verify_image_length(self) -> None:
        self._handle.seek(0, os.SEEK_END)
        actual = self._handle.tell()
        expected = self.block_size * self.block_count
        if actual != expected:
            self.close()
            raise VolumeError(
                "image length inconsistent with block geometry",
                location="volume._verify_image_length",
                image_path=self.image_path,
                expected_length=expected,
                actual_length=actual,
                block_size=self.block_size,
                block_count=self.block_count,
            )

    @classmethod
    def create(cls, image_path: str, block_size: int, block_count: int,
               fill: bytes | None = None) -> "Volume":
        """Create a fresh image. Default fill is the deterministic pattern."""
        cls._validate_geometry(block_size, block_count)
        if fill is not None and (not isinstance(fill, bytes) or len(fill) != block_size):
            raise VolumeError(
                "fill must be exactly block_size bytes",
                location="volume.create",
                fill_length=len(fill) if isinstance(fill, bytes) else None,
                block_size=block_size,
            )
        os.makedirs(os.path.dirname(os.path.abspath(image_path)) or ".", exist_ok=True)
        pattern = fill if fill is not None else deterministic_block(0, block_size)
        with open(image_path, "wb") as handle:
            for _ in range(block_count):
                handle.write(pattern)
            handle.flush()
            os.fsync(handle.fileno())
        meta = {
            "block_size": int(block_size),
            "block_count": int(block_count),
            "volume_version": 0,
            "image_digest": sha256_file(image_path),
        }
        _write_json(image_path + META_SUFFIX, meta)
        return cls(image_path, block_size, block_count)

    @classmethod
    def load(cls, image_path: str) -> "Volume":
        meta_path = image_path + META_SUFFIX
        if not os.path.exists(meta_path):
            raise VolumeError(
                "volume meta file does not exist",
                location="volume.load",
                meta_path=os.path.abspath(meta_path),
            )
        with open(meta_path, "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        try:
            block_size = int(meta["block_size"])
            block_count = int(meta["block_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VolumeError(
                "malformed volume meta file",
                location="volume.load",
                meta_path=os.path.abspath(meta_path),
                detail=str(exc),
            ) from exc
        return cls(image_path, block_size, block_count)

    # ------------------------------------------------------------------ io
    def _check_index(self, block_index: int) -> None:
        if not isinstance(block_index, int) or isinstance(block_index, bool):
            raise VolumeError(
                "block index must be an integer",
                location="volume._check_index",
                block_index=repr(block_index),
            )
        if not 0 <= block_index < self.block_count:
            raise VolumeError(
                "block index out of range",
                location="volume._check_index",
                block_index=block_index,
                block_count=self.block_count,
            )

    def read_block(self, block_index: int) -> bytes:
        self._check_index(block_index)
        self._handle.seek(block_index * self.block_size)
        data = self._handle.read(self.block_size)
        if len(data) != self.block_size:  # pragma: no cover - guarded by length check
            raise VolumeError(
                "short read from volume image",
                location="volume.read_block",
                block_index=block_index,
            )
        return data

    def write_block(self, block_index: int, data: bytes) -> None:
        self._check_index(block_index)
        if not isinstance(data, bytes) or len(data) != self.block_size:
            raise VolumeError(
                "block data must be exactly block_size bytes",
                location="volume.write_block",
                block_index=block_index,
                expected_length=self.block_size,
                actual_length=len(data) if isinstance(data, bytes) else None,
            )
        self._handle.seek(block_index * self.block_size)
        self._handle.write(data)

    def hex_to_block(self, block_index: int, hex_string: str) -> bytes:
        """Validate a hex payload and return the zero-padded block bytes."""
        self._check_index(block_index)
        if not isinstance(hex_string, str):
            raise VolumeError(
                "hex payload must be a string",
                location="volume.hex_to_block",
                block_index=block_index,
            )
        try:
            payload = bytes.fromhex(hex_string)
        except ValueError as exc:
            raise VolumeError(
                "invalid hexadecimal payload",
                location="volume.hex_to_block",
                block_index=block_index,
                hex_string=hex_string,
                detail=str(exc),
            ) from exc
        if len(payload) > self.block_size:
            raise VolumeError(
                "hex payload larger than block size",
                location="volume.hex_to_block",
                block_index=block_index,
                payload_length=len(payload),
                block_size=self.block_size,
            )
        return payload + b"\x00" * (self.block_size - len(payload))

    def inject_hex(self, block_index: int, hex_string: str) -> bytes:
        """Deterministic hex injection (raw volume write, no CoW tracking)."""
        block = self.hex_to_block(block_index, hex_string)
        self.write_block(block_index, block)
        return block

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "Volume":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ meta
    @property
    def volume_version(self) -> int:
        return int(_read_json(self.meta_path)["volume_version"])

    def increment_version(self) -> int:
        meta = _read_json(self.meta_path)
        meta["volume_version"] = int(meta["volume_version"]) + 1
        _write_json(self.meta_path, meta)
        return int(meta["volume_version"])

    def update_image_digest(self) -> str:
        self.flush()
        digest = sha256_file(self.image_path)
        meta = _read_json(self.meta_path)
        meta["image_digest"] = digest
        _write_json(self.meta_path, meta)
        return digest

    def current_digest(self) -> str:
        self.flush()
        return sha256_file(self.image_path)

    def read_all(self) -> bytes:
        self.flush()
        with open(self.image_path, "rb") as handle:
            return handle.read()


def deterministic_block(seed: int, block_size: int) -> bytes:
    """Return a deterministic block of content derived from an integer seed."""
    base = sha256_hex(f"blocksnap-block-{int(seed)}".encode("ascii")).encode("ascii")
    repeats = (block_size + len(base) - 1) // len(base)
    return (base * repeats)[:block_size]


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
