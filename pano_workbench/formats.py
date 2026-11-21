"""Readers/writers for the three mask formats.

Dispatch rules
--------------
* bytes  -> binary header ``PANO`` + version byte (1 = v1, 2 = v2)
* str    -> JSON text, parsed then treated as dict
* dict   -> COCO-style RLE exchange format (version 3)

Error contract
--------------
* unknown version / magic            -> E_VERSION_UNSUPPORTED
* truncated header, out-of-range run,
  pixel-count mismatch               -> E_FORMAT_CORRUPT (with field offset)

All parsers are deterministic: same input bytes + same config produce
byte-identical ``ParsedMask.canonical_bytes()``.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import struct

import numpy as np

from .config import IGNORE_CLASS, WorkbenchConfig
from .errors import (
    E_FORMAT_CORRUPT,
    E_INSTANCE_DUP,
    E_PIXEL_CONFLICT,
    E_VERSION_UNSUPPORTED,
    PanoError,
)

MAGIC = b"PANO"
VERSION_V1 = 1
VERSION_V2 = 2
VERSION_RLE = 3

_HEADER = struct.Struct("<4sBBII")  # magic, version, mapping_version, width, height
HEADER_SIZE = _HEADER.size


@dataclasses.dataclass
class ParsedMask:
    """Format-normalized parse result."""

    version: int
    width: int
    height: int
    mapping_version: int
    class_map: np.ndarray                 # (H, W) uint8
    instance_map: np.ndarray | None       # (H, W) uint16 or None (v1)
    conf_map: np.ndarray | None           # (H, W) float32 or None (v1)
    ignore_mask: np.ndarray               # (H, W) bool

    def canonical_bytes(self) -> bytes:
        """Deterministic byte identity of the parse result."""
        head = json.dumps(
            {
                "version": self.version,
                "width": self.width,
                "height": self.height,
                "mapping_version": self.mapping_version,
            },
            sort_keys=True,
        ).encode("utf-8")
        parts = [head, self.class_map.astype(np.uint8).tobytes()]
        parts.append(
            self.instance_map.astype("<u2").tobytes()
            if self.instance_map is not None
            else b""
        )
        parts.append(
            self.conf_map.astype("<f4").tobytes() if self.conf_map is not None else b""
        )
        parts.append(self.ignore_mask.astype(np.uint8).tobytes())
        return b"|".join(parts)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# encoders (used by the deterministic sample generator and by tests)
# ---------------------------------------------------------------------------

def _header(version: int, mapping_version: int, width: int, height: int) -> bytes:
    return _HEADER.pack(MAGIC, version, mapping_version, width, height)


def encode_v1(class_map: np.ndarray, mapping_version: int) -> bytes:
    cls = np.ascontiguousarray(class_map, dtype=np.uint8)
    h, w = cls.shape
    return _header(VERSION_V1, mapping_version, w, h) + cls.tobytes()


def encode_v2(
    class_map: np.ndarray,
    instance_map: np.ndarray,
    conf_map: np.ndarray,
    mapping_version: int,
) -> bytes:
    cls = np.ascontiguousarray(class_map, dtype=np.uint8)
    inst = np.ascontiguousarray(instance_map, dtype="<u2")
    conf = np.ascontiguousarray(conf_map, dtype="<f4")
    h, w = cls.shape
    return (
        _header(VERSION_V2, mapping_version, w, h)
        + cls.tobytes()
        + inst.tobytes()
        + conf.tobytes()
    )


def rle_encode(binary_mask: np.ndarray) -> list[int]:
    """COCO-style counts (Fortran order, counts start with a zero-run)."""
    flat = np.asfortranarray(binary_mask.astype(np.uint8)).ravel(order="F")
    if flat.size == 0:
        return []
    change = np.where(flat[1:] != flat[:-1])[0] + 1
    bounds = np.concatenate(([0], change, [flat.size]))
    runs = np.diff(bounds)
    if flat[0] == 1:
        runs = np.concatenate(([0], runs))  # counts always open with a zero-run
    return [int(v) for v in runs]


def encode_rle_json(
    segments: list[dict],
    ignore_mask: np.ndarray,
    categories: list[dict],
    width: int,
    height: int,
    mapping_version: int,
) -> str:
    """Build the COCO-style exchange document (deterministic text)."""
    doc = {
        "magic": "PANO",
        "version": VERSION_RLE,
        "mapping_version": mapping_version,
        "width": int(width),
        "height": int(height),
        "categories": sorted(
            ({"id": int(c["id"]), "name": str(c["name"])} for c in categories),
            key=lambda c: c["id"],
        ),
        "instances": sorted(
            (
                {
                    "instance_id": int(s["instance_id"]),
                    "category_id": int(s["category_id"]),
                }
                for s in segments
            ),
            key=lambda s: s["instance_id"],
        ),
        "ignore": {"counts": rle_encode(ignore_mask)},
        "segments": [
            {
                "instance_id": int(s["instance_id"]),
                "category_id": int(s["category_id"]),
                "rle": {"counts": [int(c) for c in s["rle"]["counts"]]},
                "confidence": [round(float(v), 6) for v in s["confidence"]],
            }
            for s in sorted(segments, key=lambda s: s["instance_id"])
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# decoders
# ---------------------------------------------------------------------------

def read_mask(data, cfg: WorkbenchConfig) -> ParsedMask:
    """Dispatch on the version header / field set."""
    if isinstance(data, (bytes, bytearray)):
        return _read_binary(bytes(data), cfg)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise PanoError(
                E_FORMAT_CORRUPT, "RLE document is not valid JSON",
                field_offset=exc.pos,
            ) from exc
    if isinstance(data, dict):
        return _read_rle(data, cfg)
    raise PanoError(E_VERSION_UNSUPPORTED, f"unsupported input type {type(data)!r}")


def _read_binary(raw: bytes, cfg: WorkbenchConfig) -> ParsedMask:
    if len(raw) < HEADER_SIZE:
        raise PanoError(
            E_FORMAT_CORRUPT, "truncated header",
            field_offset=len(raw), expected=HEADER_SIZE,
        )
    magic, version, mapping_version, width, height = _HEADER.unpack(raw[:HEADER_SIZE])
    if magic != MAGIC:
        raise PanoError(
            E_VERSION_UNSUPPORTED, "unknown magic", field_offset=0,
            magic=magic.hex(),
        )
    if version == VERSION_V1:
        return _read_v1(raw, mapping_version, width, height)
    if version == VERSION_V2:
        return _read_v2(raw, mapping_version, width, height)
    raise PanoError(
        E_VERSION_UNSUPPORTED, "unknown mask version",
        field_offset=4, version=version,
    )


def _section(raw: bytes, offset: int, nbytes: int, field: str,
             exact_total: bool = False) -> bytes:
    end = offset + nbytes
    if len(raw) < end:
        raise PanoError(
            E_FORMAT_CORRUPT, f"truncated {field} payload",
            field_offset=len(raw), expected_end=end,
        )
    if exact_total and len(raw) > end:
        raise PanoError(
            E_FORMAT_CORRUPT, "pixel total exceeds image size",
            field_offset=end, extra_bytes=len(raw) - end,
        )
    return raw[offset:end]


def _read_v1(raw: bytes, mapping_version: int, width: int, height: int) -> ParsedMask:
    n = width * height
    payload = _section(raw, HEADER_SIZE, n, "v1.class_index", exact_total=True)
    cls = np.frombuffer(payload, dtype=np.uint8).reshape(height, width).copy()
    ignore = cls == IGNORE_CLASS
    return ParsedMask(
        version=VERSION_V1, width=width, height=height,
        mapping_version=mapping_version,
        class_map=cls, instance_map=None, conf_map=None, ignore_mask=ignore,
    )


def _read_v2(raw: bytes, mapping_version: int, width: int, height: int) -> ParsedMask:
    n = width * height
    cls_b = _section(raw, HEADER_SIZE, n, "v2.class")
    inst_b = _section(raw, HEADER_SIZE + n, n * 2, "v2.instance")
    conf_b = _section(raw, HEADER_SIZE + 3 * n, n * 4, "v2.confidence",
                      exact_total=True)
    cls = np.frombuffer(cls_b, dtype=np.uint8).reshape(height, width).copy()
    inst = np.frombuffer(inst_b, dtype="<u2").reshape(height, width).copy()
    conf = np.frombuffer(conf_b, dtype="<f4").reshape(height, width).copy()
    ignore = cls == IGNORE_CLASS
    return ParsedMask(
        version=VERSION_V2, width=width, height=height,
        mapping_version=mapping_version,
        class_map=cls, instance_map=inst, conf_map=conf, ignore_mask=ignore,
    )


def rle_decode(counts: list, size: int, field_offset_base: str = "counts") -> np.ndarray:
    """Decode COCO counts; raises E_FORMAT_CORRUPT with the run offset."""
    out = np.zeros(size, dtype=np.uint8)
    pos = 0
    value = 0
    for i, c in enumerate(counts):
        if not isinstance(c, int) or isinstance(c, bool) or c < 0:
            raise PanoError(
                E_FORMAT_CORRUPT, "run length must be a non-negative integer",
                field_offset=f"{field_offset_base}[{i}]", value=repr(c),
            )
        if pos + c > size:
            raise PanoError(
                E_FORMAT_CORRUPT, "run exceeds image bounds",
                field_offset=f"{field_offset_base}[{i}]",
                cumulative=pos + c, image_size=size,
            )
        if value:
            out[pos:pos + c] = 1
        pos += c
        value ^= 1
    if pos != size:
        raise PanoError(
            E_FORMAT_CORRUPT, "pixel total does not match image size",
            field_offset=f"{field_offset_base}[{len(counts) - 1 if counts else 0}]",
            decoded=pos, image_size=size,
        )
    return out


def _read_rle(doc: dict, cfg: WorkbenchConfig) -> ParsedMask:
    version = doc.get("version")
    if version != VERSION_RLE:
        raise PanoError(
            E_VERSION_UNSUPPORTED, "unknown RLE document version",
            field_offset="version", version=version,
        )
    try:
        width = int(doc["width"])
        height = int(doc["height"])
        mapping_version = int(doc.get("mapping_version", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise PanoError(
            E_FORMAT_CORRUPT, "RLE header fields missing or invalid",
            field_offset=str(exc),
        ) from exc
    size = width * height

    ignore_flat = rle_decode(doc.get("ignore", {}).get("counts", [size]), size,
                             "ignore.counts")
    ignore = ignore_flat.reshape((height, width), order="F").astype(bool)

    cls = np.zeros(size, dtype=np.uint8)
    inst = np.zeros(size, dtype=np.uint16)
    conf = np.zeros(size, dtype=np.float32)
    seen_instances: dict[int, int] = {}
    for si, seg in enumerate(doc.get("segments", [])):
        iid = int(seg["instance_id"])
        cid = int(seg["category_id"])
        if iid in seen_instances:
            raise PanoError(
                E_INSTANCE_DUP, "duplicate instance id in RLE segments",
                field_offset=f"segments[{si}].instance_id", instance_id=iid,
                first_seen_at=seen_instances[iid],
            )
        seen_instances[iid] = si
        mask_flat = rle_decode(seg["rle"]["counts"], size,
                               f"segments[{si}].rle.counts")
        conf_values = seg.get("confidence", [])
        n_pix = int(mask_flat.sum())
        if len(conf_values) != n_pix:
            raise PanoError(
                E_FORMAT_CORRUPT,
                "confidence list length does not match segment pixel count",
                field_offset=f"segments[{si}].confidence",
                expected=n_pix, actual=len(conf_values),
            )
        idx = mask_flat.astype(bool)
        if (inst[idx] != 0).any():
            first = int(np.flatnonzero(idx & (inst != 0))[0])
            raise PanoError(
                E_PIXEL_CONFLICT, "RLE segments overlap on a pixel",
                field_offset=f"segments[{si}]", flat_pixel=first,
                instance_id=iid, existing_instance=int(inst[first]),
            )
        cls[idx] = cid
        inst[idx] = iid
        conf[idx] = np.asarray(conf_values, dtype=np.float32)

    cls2d = cls.reshape((height, width), order="F")
    inst2d = inst.reshape((height, width), order="F")
    conf2d = conf.reshape((height, width), order="F")
    cls2d[ignore] = IGNORE_CLASS
    inst2d[ignore] = 0
    conf2d[ignore] = 0.0
    return ParsedMask(
        version=VERSION_RLE, width=width, height=height,
        mapping_version=mapping_version,
        class_map=cls2d, instance_map=inst2d, conf_map=conf2d,
        ignore_mask=ignore,
    )
