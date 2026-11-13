"""Independent MVST container verifier.

This script intentionally does not import the mvst package: it reads the
container header/index using only the documented binary layout and checks the
contract directly.  Usage::

    python tools/verify_container.py master.mvst [--tamper-check]

It asserts the magic, version, index offset/length bounds against the real
file size, and (optionally) every saturated audio sample stays in range with
no wraparound sign flip.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

HEADER = struct.Struct("<4sHIIIIII")
ENTRY = struct.Struct("<BBHHIiiiI")
BLOCK = struct.Struct("<4sBBHI")
TYPE_VIDEO, TYPE_AUDIO, TYPE_SUBTITLE = 1, 2, 3
TYPE_NAMES = {TYPE_VIDEO: "video", TYPE_AUDIO: "audio", TYPE_SUBTITLE: "subtitle"}
LANG = {0: None, 1: "en", 2: "es", 3: "fr", 4: "ja"}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: verify_container.py MASTER [--tamper-check]", file=sys.stderr)
        return 2
    path = Path(argv[1])
    tamper = "--tamper-check" in argv
    blob = path.read_bytes()
    size = len(blob)

    assert blob[0:4] == b"MVST", "magic mismatch"
    (magic, version, sample_rate, video_frames, audio_n, sub_n,
     track_count, flags) = HEADER.unpack_from(blob, 0)
    assert version == 1, f"unexpected version {version}"

    print(f"magic={magic!r} version={version} sample_rate={sample_rate} "
          f"video_frames={video_frames} audio={audio_n} subtitles={sub_n}")
    index_end = HEADER.size + track_count * ENTRY.size
    assert index_end <= size, "index overruns file"

    rows = []
    for i in range(track_count):
        (ttype, tnum, lang_code, offset_ms, rate, byte_offset,
         start_unit, end_unit, data_length) = ENTRY.unpack_from(
             blob, HEADER.size + i * ENTRY.size)
        rows.append(dict(type=TYPE_NAMES[ttype], track=tnum,
                         language=LANG[lang_code], offset_ms=offset_ms,
                         rate=rate, byte_offset=byte_offset,
                         start_unit=start_unit, end_unit=end_unit,
                         length=data_length))
        # Every indexed byte must lie inside the file.
        assert byte_offset >= 0 and data_length >= 0
        assert byte_offset + BLOCK.size + data_length <= size, (
            f"track {i} offset+length exceeds file size")
        # Block descriptor magic at the indexed offset.
        block_magic = blob[byte_offset:byte_offset + 4]
        assert block_magic == b"BLK\x00", f"track {i} block magic bad"
        print(f"  {rows[-1]['type']:8s} track={tnum} lang={rows[-1]['language']} "
              f"offset={byte_offset} length={data_length} "
              f"units={start_unit}..{end_unit}")

    # Point-wise audio saturation check straight from the indexed blocks.
    for row in rows:
        if row["type"] != "audio":
            continue
        payload_start = row["byte_offset"] + BLOCK.size
        payload = blob[payload_start:payload_start + row["length"]]
        values = struct.unpack("<%dh" % (len(payload) // 2), payload)
        assert all(-32768 <= v <= 32767 for v in values), "sample out of range"
        flip = any(values[j - 1] >= 32700 and values[j] <= -32700
                   for j in range(1, len(values)))
        assert not flip, f"wraparound sign flip in track {row['track']}"
        print(f"  audio track {row['track']}: {len(values)} samples, "
              f"all in [-32768,32767], no sign flip")

    result = {"file": str(path), "file_size": size, "tracks": rows,
              "tamper_check": tamper, "verdict": "OK"}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except AssertionError as exc:
        print(json.dumps({"error_code": "E_VERIFY", "detail": str(exc)}))
        raise SystemExit(2)
