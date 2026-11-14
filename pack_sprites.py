"""Command line entry point for the sprite packer.

Usage:
    python pack_sprites.py <input_dir> [--output-dir DIR]
                           [--sheet-name sprite_sheet.png]
                           [--report-name report.json]

Exit codes:
    0  success (valid frames processed, possibly with skipped invalid frames)
    1  no valid frames (report.json still written, no sprite sheet)
    2  sequence validation failure (duplicate / missing / unparseable index,
       or input directory missing; no products left behind)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Under "python -I" the script directory is not guaranteed to be on sys.path;
# add it explicitly so the local package resolves in every launch mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sprite_packer import (
    DEFAULT_REPORT_NAME,
    DEFAULT_SHEET_NAME,
    SequenceError,
    pack,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pack an indexed PNG frame sequence into a transparent "
        "aligned RGBA sprite sheet with a JSON status report."
    )
    parser.add_argument("input_dir", help="directory containing the PNG frame sequence")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="directory for sprite_sheet.png / report.json (default: input_dir)",
    )
    parser.add_argument("--sheet-name", default=DEFAULT_SHEET_NAME)
    parser.add_argument("--report-name", default=DEFAULT_REPORT_NAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = pack(
            args.input_dir,
            output_dir=args.output_dir,
            sheet_name=args.sheet_name,
            report_name=args.report_name,
        )
    except SequenceError as exc:
        print(f"sequence error: {exc}", file=sys.stderr)
        return 2

    for item in result.invalid_frames:
        print(f"skipped invalid frame {item.file}: {item.reason}", file=sys.stderr)
    if result.valid_frame_count == 0:
        print(
            f"no valid frames among {result.input_frame_count} input PNG(s); "
            f"report written to {result.report_path}",
            file=sys.stderr,
        )
    else:
        print(
            f"packed {result.valid_frame_count}/{result.input_frame_count} frame(s) "
            f"into {result.sheet_path} ({result.sheet_width}x{result.sheet_height}, RGBA); "
            f"report: {result.report_path}"
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
