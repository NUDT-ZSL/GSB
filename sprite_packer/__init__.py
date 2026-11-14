"""Deterministic PNG frame-sequence -> transparent, aligned RGBA sprite sheet.

Pipeline:
  1. Scan all *.png files in the input directory (non-recursive).
  2. Parse exactly one integer index per file name; duplicates / gaps /
     unparseable names abort the run (SequenceError) with no outputs.
  3. Decode every indexed frame; corrupt or unidentifiable files are recorded
     as invalid frames and skipped, the remaining frames keep processing.
  4. Normalize valid frames to RGBA (missing alpha -> fully opaque; existing
     transparency is preserved).
  5. Center every frame on a transparent cell sized to the max width/height.
  6. Concatenate cells horizontally into sprite_sheet.png (RGBA) and write a
     report.json containing per-frame status and sheet X coordinates.

The module is deterministic: identical inputs always yield byte-identical
outputs (no timestamps, fixed JSON ordering, fixed PNG save options).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PIL import Image

DEFAULT_SHEET_NAME = "sprite_sheet.png"
DEFAULT_REPORT_NAME = "report.json"

# Process exit codes (also returned by pack()).
EXIT_OK = 0
EXIT_NO_VALID_FRAMES = 1
EXIT_SEQUENCE_ERROR = 2

_INDEX_RE = re.compile(r"\d+")


class SequenceError(ValueError):
    """Raised when frame indices are missing, duplicated, or unparseable."""


@dataclass
class InvalidFrame:
    file: str
    reason: str


@dataclass
class FrameRecord:
    index: int
    file: str
    x: int
    width: int
    height: int
    offset_x: int
    offset_y: int
    original_mode: str


@dataclass
class PackResult:
    exit_code: int
    input_frame_count: int
    valid_frame_count: int
    invalid_frames: list[InvalidFrame] = field(default_factory=list)
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    sheet_width: Optional[int] = None
    sheet_height: Optional[int] = None
    frames: list[FrameRecord] = field(default_factory=list)
    sheet_path: Optional[Path] = None
    report_path: Optional[Path] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        if self.valid_frame_count == 0:
            sheet_info = None
        else:
            sheet_info = {
                "file": self.sheet_path.name if self.sheet_path else DEFAULT_SHEET_NAME,
                "width": self.sheet_width,
                "height": self.sheet_height,
                "mode": "RGBA",
            }
        return {
            "input_frame_count": self.input_frame_count,
            "valid_frame_count": self.valid_frame_count,
            "invalid_frames": [
                {"file": item.file, "reason": item.reason} for item in self.invalid_frames
            ],
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "sprite_sheet": sheet_info,
            "frames": [
                {
                    "index": item.index,
                    "file": item.file,
                    "x": item.x,
                    "width": item.width,
                    "height": item.height,
                    "offset_x": item.offset_x,
                    "offset_y": item.offset_y,
                    "original_mode": item.original_mode,
                }
                for item in self.frames
            ],
        }


def discover_pngs(input_dir: Path, excluded_names: set[str]) -> list[Path]:
    """Return all top-level *.png files (case-insensitive suffix), sorted by name."""
    return sorted(
        (
            path
            for path in input_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".png"
            and path.name not in excluded_names
        ),
        key=lambda path: path.name,
    )


def parse_indices(png_paths: list[Path]) -> dict[int, Path]:
    """Map each PNG to its unique integer index; raise SequenceError on problems.

    A valid file name contains exactly one run of digits in its stem.
    Duplicate indices and gaps inside the observed index range are errors.
    """
    if not png_paths:
        raise SequenceError("no PNG frames found in input directory")

    indices: dict[int, Path] = {}
    for path in png_paths:
        matches = _INDEX_RE.findall(path.stem)
        if len(matches) != 1:
            raise SequenceError(
                f"filename must contain exactly one integer sequence, "
                f"found {len(matches)}: {path.name}"
            )
        index = int(matches[0])
        if index in indices:
            raise SequenceError(
                f"duplicate frame index {index}: {indices[index].name} and {path.name}"
            )
        indices[index] = path

    lo, hi = min(indices), max(indices)
    missing = sorted(set(range(lo, hi + 1)) - set(indices))
    if missing:
        raise SequenceError(
            "missing frame index(es): " + ", ".join(str(i) for i in missing)
        )
    return indices


def load_rgba(path: Path) -> tuple[Image.Image, str]:
    """Decode a PNG and return (RGBA image, original mode).

    verify() plus a full load() reject truncated / structurally broken files;
    convert("RGBA") fills opaque alpha for RGB/L/P frames without transparency
    and preserves existing alpha for RGBA/LA/P-with-transparency frames.
    """
    with Image.open(path) as probe:
        probe.verify()
    with Image.open(path) as img:
        img.load()
        original_mode = img.mode
        rgba = img if img.mode == "RGBA" else img.convert("RGBA")
        return rgba.copy(), original_mode


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _write_report(report_path: Path, report: dict) -> None:
    with open(report_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def pack(
    input_dir: str | Path,
    output_dir: str | Path | None = None,
    sheet_name: str = DEFAULT_SHEET_NAME,
    report_name: str = DEFAULT_REPORT_NAME,
) -> PackResult:
    """Run the full packing pipeline. See module docstring for semantics."""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise SequenceError(f"input directory not found: {input_dir}")

    output_dir = Path(output_dir) if output_dir is not None else input_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    sheet_path = output_dir / sheet_name
    report_path = output_dir / report_name

    # A stale sheet inside the input directory must never be treated as a
    # frame, even when outputs are written elsewhere (requirement 9).
    png_paths = discover_pngs(input_dir, {sheet_name})
    try:
        indices = parse_indices(png_paths)
    except SequenceError:
        # A failed validation run must leave no products behind, including
        # stale ones from an earlier successful run.
        _remove_if_exists(sheet_path)
        _remove_if_exists(report_path)
        raise

    invalid_frames: list[InvalidFrame] = []
    decoded: list[tuple[int, Path, Image.Image, str]] = []
    for index, path in sorted(indices.items()):
        try:
            rgba, original_mode = load_rgba(path)
        except Exception as exc:  # corrupt / unidentifiable / truncated PNG
            invalid_frames.append(
                InvalidFrame(file=path.name, reason=f"{type(exc).__name__}: {exc}")
            )
            continue
        decoded.append((index, path, rgba, original_mode))

    if not decoded:
        _remove_if_exists(sheet_path)
        result = PackResult(
            exit_code=EXIT_NO_VALID_FRAMES,
            input_frame_count=len(png_paths),
            valid_frame_count=0,
            invalid_frames=invalid_frames,
            sheet_path=sheet_path,
            report_path=report_path,
        )
        _write_report(report_path, result.to_dict())
        return result

    cell_width = max(item[2].width for item in decoded)
    cell_height = max(item[2].height for item in decoded)

    sheet = Image.new("RGBA", (cell_width * len(decoded), cell_height), (0, 0, 0, 0))
    records: list[FrameRecord] = []
    for slot, (index, path, rgba, original_mode) in enumerate(decoded):
        offset_x = (cell_width - rgba.width) // 2
        offset_y = (cell_height - rgba.height) // 2
        cell = Image.new("RGBA", (cell_width, cell_height), (0, 0, 0, 0))
        cell.paste(rgba, (offset_x, offset_y))
        x = slot * cell_width
        sheet.paste(cell, (x, 0))
        records.append(
            FrameRecord(
                index=index,
                file=path.name,
                x=x,
                width=rgba.width,
                height=rgba.height,
                offset_x=offset_x,
                offset_y=offset_y,
                original_mode=original_mode,
            )
        )

    sheet.save(sheet_path, format="PNG")

    result = PackResult(
        exit_code=EXIT_OK,
        input_frame_count=len(png_paths),
        valid_frame_count=len(decoded),
        invalid_frames=invalid_frames,
        frame_width=cell_width,
        frame_height=cell_height,
        sheet_width=sheet.width,
        sheet_height=sheet.height,
        frames=records,
        sheet_path=sheet_path,
        report_path=report_path,
    )
    _write_report(report_path, result.to_dict())
    return result
