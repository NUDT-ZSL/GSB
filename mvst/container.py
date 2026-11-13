"""MVST binary container: fixed header index followed by addressed data blocks.

On-disk layout::

    header  (fixed 56 bytes, starts with magic b"MVST")
    index   (one 40-byte entry per track, order: video, audio..., subtitle...)
    blocks  (video block, audio blocks..., subtitle blocks...)

Every block is preceded by a 16-byte descriptor (magic, type, track number,
data length) and padded to 8-byte alignment.  All offsets/lengths recorded in
the index are the actual byte positions used at write time, so ``info`` can
answer entirely from the header and never has to scan block payloads -
tampering an audio block's data bytes therefore cannot change ``info`` output.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path

from . import CONTAINER_VERSION, MAGIC, MASTER_SAMPLE_RATE, VIDEO_FPS
from .errors import MvstError

HEADER_STRUCT = struct.Struct("<4sHIIIIII")       # 32 bytes
ENTRY_STRUCT = struct.Struct("<BBHHIiiiI")        # 26 bytes
BLOCK_STRUCT = struct.Struct("<4sBBHI")           # 12 bytes
HEADER_SIZE = HEADER_STRUCT.size
ENTRY_SIZE = ENTRY_STRUCT.size
BLOCK_HEADER_SIZE = BLOCK_STRUCT.size
BLOCK_MAGIC = b"BLK\x00"

TYPE_VIDEO = 1
TYPE_AUDIO = 2
TYPE_SUBTITLE = 3
ALIGNMENT = 8


@dataclass
class IndexEntry:
    track_type: int
    track_number: int
    language_code: int          # 0 = n/a; otherwise 1..4 for en/es/fr/ja
    offset_ms: int
    sample_rate: int
    byte_offset: int
    start_unit: int             # first sample / first frame
    end_unit: int               # last sample / last frame (inclusive)
    data_length: int


_LANG_CODES = {"en": 1, "es": 2, "fr": 3, "ja": 4}
_CODE_LANG = {v: k for k, v in _LANG_CODES.items()}


def language_code(language: str | None) -> int:
    return _LANG_CODES.get(language or "", 0)


def code_language(code: int) -> str | None:
    return _CODE_LANG.get(code)


def _pad_position(position: int) -> int:
    remainder = position % ALIGNMENT
    return position if remainder == 0 else position + ALIGNMENT - remainder


def encode_subtitle_block(cues: list[tuple[int, int, int, str]]) -> bytes:
    """Encode (seq, start_frame, end_frame, text) cues into a subtitle block."""
    parts: list[bytes] = [struct.pack("<I", len(cues))]
    for seq, start_frame, end_frame, text in cues:
        payload = text.encode("utf-8")
        parts.append(struct.pack("<IIIH", seq, start_frame, end_frame, len(payload)))
        parts.append(payload)
    return b"".join(parts)


def decode_subtitle_block(blob: bytes) -> list[tuple[int, int, int, str]]:
    count = struct.unpack_from("<I", blob, 0)[0]
    cursor = 4
    out: list[tuple[int, int, int, str]] = []
    for _ in range(count):
        seq, start_frame, end_frame, text_len = struct.unpack_from("<IIIH", blob, cursor)
        cursor += 14
        text = blob[cursor:cursor + text_len].decode("utf-8")
        cursor += text_len
        out.append((seq, start_frame, end_frame, text))
    return out


def _slow_write_hook() -> None:
    # Test hook: gives an external Ctrl+C a chance to land mid-write.
    if os.environ.get("MVST_SLOW_WRITE") == "1":
        time.sleep(0.25)


def write_master(
    out_path: str | Path,
    video_frames: int,
    video_bytes: bytes,
    audio_payloads: list[tuple[IndexEntry, bytes]],
    subtitle_payloads: list[tuple[IndexEntry, bytes]],
) -> None:
    """Write the container to a sibling temp file then atomically rename."""
    out_path = Path(out_path)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    track_count = 1 + len(audio_payloads) + len(subtitle_payloads)
    entries: list[IndexEntry] = []
    blocks: list[tuple[int, int, bytes]] = []

    data_start = HEADER_SIZE + track_count * ENTRY_SIZE
    position = data_start

    def place(track_type: int, track_number: int, payload: bytes) -> None:
        nonlocal position
        block_header = BLOCK_STRUCT.pack(
            BLOCK_MAGIC, track_type, track_number, 0, len(payload)
        )
        position += BLOCK_HEADER_SIZE
        blocks.append((track_type, track_number, block_header + payload))
        position += len(payload)
        position = _pad_position(position)

    # Reserve index entries while computing offsets in block order.
    video_entry = IndexEntry(
        track_type=TYPE_VIDEO, track_number=0, language_code=0,
        offset_ms=0, sample_rate=VIDEO_FPS, byte_offset=0,
        start_unit=0, end_unit=max(video_frames - 1, 0),
        data_length=len(video_bytes),
    )
    entries.append(video_entry)
    video_offset = position
    place(TYPE_VIDEO, 0, video_bytes)
    video_entry.byte_offset = video_offset

    for entry, payload in audio_payloads:
        entry.byte_offset = position
        entries.append(entry)
        place(TYPE_AUDIO, entry.track_number, payload)

    for entry, payload in subtitle_payloads:
        entry.byte_offset = position
        entries.append(entry)
        place(TYPE_SUBTITLE, entry.track_number, payload)

    try:
        with open(tmp_path, "wb") as fh:
            fh.write(HEADER_STRUCT.pack(
                MAGIC, CONTAINER_VERSION, MASTER_SAMPLE_RATE, video_frames,
                len(audio_payloads), len(subtitle_payloads), track_count, 0,
            ))
            for entry in entries:
                fh.write(ENTRY_STRUCT.pack(
                    entry.track_type, entry.track_number, entry.language_code,
                    entry.offset_ms, entry.sample_rate,
                    entry.byte_offset, entry.start_unit, entry.end_unit,
                    entry.data_length,
                ))
            for track_type, track_number, block in blocks:
                _slow_write_hook()
                fh.write(block)
                current = fh.tell()
                padded = _pad_position(current)
                if padded != current:
                    fh.write(b"\x00" * (padded - current))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_path)
    except BaseException:
        # Ctrl+C and any write failure: remove the partial temp file, leave an
        # existing target untouched.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


@dataclass
class MasterInfo:
    version: int
    sample_rate: int
    video_frames: int
    audio_track_count: int
    subtitle_track_count: int
    entries: list[IndexEntry]


def read_master_header(path: str | Path) -> MasterInfo:
    """Read only the fixed header + index (no block scanning)."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        raise MvstError("E_MASTER_INVALID", file=str(path))
    if len(blob) < HEADER_SIZE or blob[0:4] != MAGIC:
        raise MvstError("E_MASTER_INVALID", file=str(path))
    (magic, version, sample_rate, video_frames,
     audio_count, subtitle_count, track_count, _flags) = HEADER_STRUCT.unpack_from(blob, 0)
    if version != CONTAINER_VERSION:
        raise MvstError("E_MASTER_INVALID", file=str(path))
    entries: list[IndexEntry] = []
    for i in range(track_count):
        cursor = HEADER_SIZE + i * ENTRY_SIZE
        if cursor + ENTRY_SIZE > len(blob):
            raise MvstError("E_MASTER_INVALID", file=str(path))
        (track_type, track_number, lang_code, offset_ms, sample_rate_i,
         byte_offset, start_unit, end_unit,
         data_length) = ENTRY_STRUCT.unpack_from(blob, cursor)
        entries.append(IndexEntry(
            track_type=track_type,
            track_number=track_number,
            language_code=lang_code,
            offset_ms=offset_ms,
            sample_rate=sample_rate_i,
            byte_offset=byte_offset,
            start_unit=start_unit,
            end_unit=end_unit,
            data_length=data_length,
        ))
    return MasterInfo(
        version=version,
        sample_rate=sample_rate,
        video_frames=video_frames,
        audio_track_count=audio_count,
        subtitle_track_count=subtitle_count,
        entries=entries,
    )


def extract_block(path: str | Path, entry: IndexEntry) -> bytes:
    """Extract one block payload using the index offset/length only."""
    with open(path, "rb") as fh:
        fh.seek(entry.byte_offset)
        header = fh.read(BLOCK_HEADER_SIZE)
        if len(header) != BLOCK_HEADER_SIZE or header[0:4] != BLOCK_MAGIC:
            raise MvstError("E_MASTER_INVALID", file=str(path))
        return fh.read(entry.data_length)
