"""Report construction and JSON serialisation.

Report status values
--------------------
``complete``
    Every manifest clip was mixed into the output WAV.
``partial``
    The WAV was produced, but one or more source files were missing or
    unreadable and were excluded from the mix.
``invalid``
    The manifest failed schema validation (bad offset, gain, or structure).
``failed``
    Processing stopped before mixing: source format mismatch, or no usable
    clips at all.

The ``clips`` array is always ordered by ``offset_ms`` ascending (ties keep
manifest order).  Each entry carries a boolean ``clipped`` flag once the clip
has been mixed; excluded entries use ``null``.
"""

from __future__ import annotations

import json
from pathlib import Path


def sort_clip_reports(clips: list[dict]) -> list[dict]:
    return sorted(clips, key=lambda item: (item["offset_ms"], item["index"]))


def write_report(path: Path | str, report: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def make_invalid_report(
    *,
    manifest_path: Path,
    errors: list[dict],
    requested: int | None = None,
) -> dict:
    return {
        "tool": "wavmixer",
        "status": "invalid",
        "reason": "manifest_invalid",
        "manifest": str(manifest_path),
        "output": None,
        "total_duration_ms": None,
        "overall_clipped": None,
        "format": None,
        "summary": {"requested": requested, "mixed": 0, "excluded": 0},
        "clips": [],
        "excluded_sources": [],
        "format_mismatches": [],
        "errors": errors,
    }


def make_failure_report(
    *,
    manifest_path: Path,
    reason: str,
    clips: list[dict],
    errors: list[dict],
    excluded_sources: list[dict] | None = None,
    format_mismatches: list[dict] | None = None,
    requested: int | None = None,
) -> dict:
    return {
        "tool": "wavmixer",
        "status": "failed",
        "reason": reason,
        "manifest": str(manifest_path),
        "output": None,
        "total_duration_ms": None,
        "overall_clipped": None,
        "format": None,
        "summary": {
            "requested": len(clips) if requested is None else requested,
            "mixed": 0,
            "excluded": len(excluded_sources or []),
        },
        "clips": sort_clip_reports(clips),
        "excluded_sources": excluded_sources or [],
        "format_mismatches": format_mismatches or [],
        "errors": errors,
    }


def make_success_report(
    *,
    manifest_path: Path,
    output_path: Path,
    total_duration_ms: int,
    overall_clipped: bool,
    sample_rate: int,
    channels: int,
    frame_count: int,
    clips: list[dict],
    excluded_sources: list[dict],
) -> dict:
    mixed = sum(1 for item in clips if item["status"] == "mixed")
    return {
        "tool": "wavmixer",
        "status": "complete" if not excluded_sources else "partial",
        "reason": None,
        "manifest": str(manifest_path),
        "output": str(output_path),
        "total_duration_ms": total_duration_ms,
        "overall_clipped": overall_clipped,
        "format": {
            "sample_rate": sample_rate,
            "channels": channels,
            "bits_per_sample": 16,
            "frames": frame_count,
        },
        "summary": {
            "requested": len(clips),
            "mixed": mixed,
            "excluded": len(excluded_sources),
        },
        "clips": sort_clip_reports(clips),
        "excluded_sources": excluded_sources,
        "format_mismatches": [],
        "errors": [],
    }
