"""Command-line entry point for the tile pyramid generator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .core import (
    ConfigError,
    ManifestError,
    SourceError,
    TilemapError,
    generate_tiles,
)

__all__ = ["build_parser", "main"]


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value}")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tilemap",
        description=(
            "Split a source image into a PNG tile pyramid named z_x_y.png "
            "and write a JSON manifest describing every level and tile."
        ),
    )
    parser.add_argument("source", help="source image path")
    parser.add_argument("output", help="output directory for tiles and manifest")
    parser.add_argument(
        "--tile-size",
        type=_positive_int,
        default=256,
        help="tile edge length in pixels (default: 256)",
    )
    parser.add_argument(
        "--start-level",
        type=_non_negative_int,
        default=0,
        help="first level to generate; lower levels are skipped (default: 0)",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="manifest path (default: <output>/manifest.json)",
    )
    parser.add_argument(
        "--max-pixels",
        type=int,
        default=-1,
        metavar="N",
        help=(
            "pixel-count guard for very large sources; -1 = Pillow default, "
            "0 = no limit for trusted maps, N = explicit limit"
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-level progress lines on stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def report_level(level: dict) -> None:
        if args.quiet:
            return
        if level["skipped"]:
            print(
                f"level {level['level']}: skipped total=0 success=0 "
                f"empty=0 failed=0 (start_level={args.start_level})",
                flush=True,
            )
            return
        print(
            f"level {level['level']}: total={level['total']} "
            f"success={level['success']} empty={level['empty']} "
            f"failed={level['failed']} grid={level['cols']}x{level['rows']}",
            flush=True,
        )

    def report_tile_failure(tile: dict) -> None:
        print(
            f"warning: level {tile['level']} tile "
            f"x={tile['x']} y={tile['y']} failed: {tile['error']}",
            file=sys.stderr,
            flush=True,
        )

    try:
        manifest = generate_tiles(
            args.source,
            args.output,
            tile_size=args.tile_size,
            start_level=args.start_level,
            manifest_path=args.manifest,
            max_pixels=args.max_pixels,
            on_level=report_level,
            on_tile_error=report_tile_failure,
        )
    except (SourceError, ConfigError, ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    except TilemapError as exc:  # pragma: no cover - defensive
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    summary = manifest["summary"]
    manifest_path = (
        Path(args.manifest)
        if args.manifest is not None
        else Path(args.output) / "manifest.json"
    )
    print(
        f"summary: levels={len(manifest['levels'])} total={summary['total']} "
        f"success={summary['success']} empty={summary['empty']} "
        f"failed={summary['failed']} manifest={manifest_path.as_posix()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
