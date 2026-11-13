#!/usr/bin/env python3
"""Generate the deterministic WAV sample set used by the demo and tests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import wav_samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make_samples.py",
        description="Generate deterministic 16-bit PCM WAV fixtures.",
    )
    parser.add_argument(
        "--output-dir",
        default="demo/samples",
        help="directory that receives the WAV files (default: demo/samples)",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite existing files"
    )
    args = parser.parse_args(argv)

    directory = Path(args.output_dir)
    if directory.exists() and not args.force:
        existing = [p for p in directory.glob("*.wav")]
        if existing and not (directory / ".complete").exists():
            print(
                f"ERROR: {directory} already contains WAV files; "
                "pass --force to regenerate them",
                file=sys.stderr,
            )
            return 2

    files = wav_samples.create_sample_set(directory)
    (directory / ".complete").write_text("generated\n", encoding="utf-8")
    print(f"Created {len(files)} sample WAV files in {directory.resolve()}:")
    for line in wav_samples.describe_sample_set(files):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
