#!/usr/bin/env python3
"""make_samples.py - Deterministically generate test ZIP samples.

Three archives are (re)created inside <workspace>/samples:

  safe.zip       normal archive, nested directories, fixed timestamps
  malicious.zip  safe entries plus parent-traversal and absolute-path entries
  corrupt.zip    a valid archive whose central bytes are deterministically
                 overwritten so it cannot be opened / read

The script only uses the standard library and always produces byte-identical
outputs for the same Python version (fixed content, timestamps and RNG seed).
"""

from __future__ import annotations

import os
import random
import shutil
import sys
import zipfile

# Fixed DOS-style timestamp embedded in every generated entry:
# (year, month, day, hour, minute, second) -> 2025-01-15 08:30:45 UTC.
FIXED_DATE = (2025, 1, 15, 8, 30, 45)

# Entry name -> payload for the well-behaved archive.
SAFE_ENTRIES = [
    ("readme.txt", b"safe archive - staging demo\n"),
    ("docs/intro.md", b"# intro\n\nnested directory entry\n"),
    ("docs/data/values.txt", b"one\ntwo\nthree\n"),
    ("empty_dir/", None),
]

# Archive order is significant: tests assert unsafe entries keep this order.
# Includes both POSIX and Windows style traversal / absolute entries.
MALICIOUS_ENTRIES = [
    ("good/start.txt", b"first safe entry\n"),
    ("../escape.txt", b"parent traversal payload\n"),
    ("/tmp/absolute_posix.txt", b"absolute posix payload\n"),
    ("good/middle.txt", b"second safe entry\n"),
    ("..\\windows_escape.txt", b"backslash traversal payload\n"),
    ("C:/windows_drive.txt", b"windows drive payload\n"),
    ("good/deep/end.txt", b"third safe entry\n"),
]


def _add_entry(zf: zipfile.ZipFile, name: str, payload: bytes | None) -> None:
    info = zipfile.ZipInfo(filename=name, date_time=FIXED_DATE)
    if payload is None:
        # Explicit directory entry.
        info.external_attr = (0o755 << 16) | 0x10
        zf.writestr(info, b"")
    else:
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, payload)


def make_safe_zip(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in SAFE_ENTRIES:
            _add_entry(zf, name, payload)


def make_malicious_zip(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, payload in MALICIOUS_ENTRIES:
            _add_entry(zf, name, payload)


def make_corrupt_zip(path: str) -> None:
    """Build a valid archive then deterministically destroy inner bytes."""
    tmp_path = path + ".tmp"
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_entry(zf, "will_be_corrupt.txt", b"x" * 4096)
        _add_entry(zf, "second_entry.txt", b"y" * 4096)

    with open(tmp_path, "rb") as fh:
        data = bytearray(fh.read())

    # Seeded overwrite of local-file-header region (after the first header).
    rng = random.Random(20250115)
    start = 64
    length = min(256, len(data) // 2)
    for offset in range(start, start + length):
        data[offset] = rng.randrange(256)

    with open(path, "wb") as fh:
        fh.write(data)
    os.remove(tmp_path)


def main() -> int:
    workspace = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "demo_workspace"
    )
    samples = os.path.join(workspace, "samples")
    if os.path.isdir(samples):
        shutil.rmtree(samples)
    os.makedirs(samples, exist_ok=True)

    make_safe_zip(os.path.join(samples, "safe.zip"))
    make_malicious_zip(os.path.join(samples, "malicious.zip"))
    make_corrupt_zip(os.path.join(samples, "corrupt.zip"))

    for name in ("safe.zip", "malicious.zip", "corrupt.zip"):
        full = os.path.join(samples, name)
        print(f"generated {full} ({os.path.getsize(full)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
