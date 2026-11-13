#!/usr/bin/env python3
"""Assemble a podcast episode from 16-bit PCM mono 44.1 kHz WAV segments.

The tool is deliberately dependency-free (Python standard library only).
It reads a JSON manifest, validates and places each segment on a sample
timeline, skips bad segments while recording the reason, writes the mixed
episode WAV and a UTF-8 JSON report.

Exit codes
----------
0 : assembly completed (segment-level problems are reported in the report;
    a summary is printed to stderr)
2 : the manifest itself is invalid (empty list, bad item, negative start...)
1 : the output WAV or report could not be written

Run ``python podcast_assembler.py --help`` for the command line.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import wave
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

SAMPLE_RATE = 44100
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
SAMPLE_WIDTH_BITS = 16

SAMPLES_PER_MILLISECOND = Decimal(SAMPLE_RATE) / Decimal(1000)
REPORT_TIME_QUANTUM = Decimal("0.001")

_MISSING = object()


class ManifestError(Exception):
    """The manifest as a whole is invalid; assembly must not start."""


class AudioFormatError(Exception):
    """A single segment file cannot be used; it is skipped, not fatal."""


class FadeError(ValueError):
    """The fade fields of a single manifest item are malformed."""


@dataclass(frozen=True)
class SegmentSpec:
    """One validated manifest entry (validation that is manifest-fatal)."""

    manifest_index: int
    path: str                     # exactly as written in the manifest
    resolved_path: str            # absolute path used for I/O
    start: Decimal                # start seconds, exact decimal
    start_sample: int             # rounded sample position
    fade_in_raw: Any = _MISSING
    fade_out_raw: Any = _MISSING


@dataclass
class Issue:
    manifest_index: int
    path: str
    code: str
    description: str


@dataclass
class AcceptedSegment:
    spec: SegmentSpec
    start_sample: int
    end_sample: int
    samples: array.array
    fade_in_ms: Decimal
    fade_out_ms: Decimal
    fade_in_samples: int
    fade_out_samples: int


@dataclass
class AssemblyResult:
    segments: List[AcceptedSegment]          # sorted by start_sample
    issues: List[Issue]                      # manifest order
    silences: List[Tuple[int, int]]          # sample intervals, ascending
    total_samples: int


# ---------------------------------------------------------------------------
# Time conversion
# ---------------------------------------------------------------------------

def _is_number(value: Any) -> bool:
    """True for JSON numbers, excluding booleans (bool is an int subclass)."""
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(int(value))


def seconds_to_samples(seconds: Decimal) -> int:
    """Convert seconds to samples, rounding half away from zero (non-negative)."""
    return int(
        (seconds * Decimal(SAMPLE_RATE)).to_integral_value(rounding=ROUND_HALF_UP)
    )


def milliseconds_to_samples(milliseconds: Decimal) -> int:
    return int(
        (milliseconds * SAMPLES_PER_MILLISECOND).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )


def samples_to_seconds(samples: int) -> float:
    """Convert a sample position to seconds, quantised to 0.001 s for reports."""
    value = (Decimal(samples) / Decimal(SAMPLE_RATE)).quantize(
        REPORT_TIME_QUANTUM, rounding=ROUND_HALF_UP
    )
    return float(value)


def _round_half_up(value: float) -> int:
    """Round half away from zero (symmetric for negative samples)."""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return -int(math.floor(-value + 0.5))


def _format_decimal(value: Decimal) -> str:
    """Compact, human readable decimal for issue messages."""
    text = format(value.normalize(), "f")
    return text


# ---------------------------------------------------------------------------
# Manifest handling
# ---------------------------------------------------------------------------

def _reject_constant(token: str) -> None:
    raise ValueError(f"non-finite number {token!r} is not allowed")


def load_manifest(manifest_path: os.PathLike | str) -> List[SegmentSpec]:
    """Parse and structurally validate a manifest.

    Only manifest-level problems raise :class:`ManifestError`.  Malformed
    optional fade values are per-segment issues and are handled later.
    """
    manifest_path = os.fspath(manifest_path)
    try:
        with open(manifest_path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(
                handle,
                parse_float=Decimal,
                parse_constant=_reject_constant,
            )
    except OSError as exc:
        raise ManifestError(f"cannot read {manifest_path}: {exc}") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ManifestError(f"malformed JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ManifestError("top level must be a JSON object")

    entries = payload.get("segments")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("'segments' must be a non-empty list")

    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    specs: List[SegmentSpec] = []

    for index, entry in enumerate(entries):
        where = f"segment {index}"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where}: must be a JSON object")

        path = entry.get("path")
        if not isinstance(path, str) or path == "":
            raise ManifestError(f"{where}: 'path' must be a non-empty string")

        if "start" not in entry:
            raise ManifestError(f"{where}: 'start' is required")
        start = entry["start"]
        if not _is_number(start) or start < 0:
            raise ManifestError(
                f"{where}: 'start' must be a non-negative number of seconds"
            )
        start_decimal = _as_decimal(start)

        resolved = os.path.normpath(os.path.join(base_dir, path))
        specs.append(
            SegmentSpec(
                manifest_index=index,
                path=path,
                resolved_path=resolved,
                start=start_decimal,
                start_sample=seconds_to_samples(start_decimal),
                fade_in_raw=entry.get("fade_in_ms", _MISSING),
                fade_out_raw=entry.get("fade_out_ms", _MISSING),
            )
        )

    return specs


# ---------------------------------------------------------------------------
# Segment reading / processing
# ---------------------------------------------------------------------------

def read_segment(path: str) -> array.array:
    """Read and validate a 16-bit PCM mono 44.1 kHz WAV file.

    Raises :class:`AudioFormatError` when the file cannot be used as a
    segment (wrong format, truncated, empty, unreadable container).
    """
    try:
        with wave.open(path, "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            frame_rate = reader.getframerate()
            compression = reader.getcomptype()
            frame_count = reader.getnframes()

            if (
                compression != "NONE"
                or channels != CHANNELS
                or sample_width != SAMPLE_WIDTH_BYTES
                or frame_rate != SAMPLE_RATE
            ):
                raise AudioFormatError(
                    "unsupported audio format "
                    f"(expected 16-bit PCM mono {SAMPLE_RATE} Hz; got "
                    f"{channels} channel(s), {frame_rate} Hz, "
                    f"{sample_width * 8} bit, compression={compression})"
                )

            raw = reader.readframes(frame_count)
    except AudioFormatError:
        raise
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise AudioFormatError(f"unreadable WAV file: {exc}") from exc

    if frame_count == 0 or len(raw) == 0:
        raise AudioFormatError("empty audio segment (0 frames)")
    if len(raw) != frame_count * SAMPLE_WIDTH_BYTES:
        raise AudioFormatError(
            f"truncated audio data ({len(raw)} bytes for {frame_count} frames)"
        )

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _fade_from_raw(raw: Any, field: str) -> Tuple[Decimal, int]:
    """Validate one optional fade field and return (milliseconds, samples)."""
    if raw is _MISSING or raw is None:
        return Decimal(0), 0
    if not _is_number(raw) or raw < 0:
        raise FadeError(f"{field} must be a non-negative number of milliseconds")
    milliseconds = _as_decimal(raw)
    return milliseconds, milliseconds_to_samples(milliseconds)


def _apply_fades(samples: array.array, fade_in: int, fade_out: int) -> None:
    """Apply linear fades in place.

    Fade-in sample ``i`` gets gain ``i / fade_in`` (so the first sample is 0);
    fade-out sample ``k`` of the tail gets gain ``(fade_out - k - 1) / fade_out``
    (so the last sample is 0).  Samples outside the fade regions are untouched.
    """
    total = len(samples)
    if fade_in:
        for index in range(fade_in):
            samples[index] = _round_half_up(samples[index] * index / fade_in)
    if fade_out:
        offset = total - fade_out
        for k in range(fade_out):
            index = offset + k
            gain = (fade_out - k - 1) / fade_out
            samples[index] = _round_half_up(samples[index] * gain)


def _intervals_overlap(
    start_a: int, end_a: int, start_b: int, end_b: int
) -> bool:
    return start_a < end_b and start_b < end_a


def assemble(specs: Sequence[SegmentSpec]) -> AssemblyResult:
    """Place all usable segments on the timeline.

    Conflict resolution follows manifest order: the first occurrence of a
    path/start pair wins over duplicates, and the first segment claiming a
    time span wins over later overlapping segments.  The returned success
    list is sorted by start sample, which is also the output WAV order.
    """
    seen_keys: Dict[Tuple[str, Decimal], int] = {}
    accepted: List[AcceptedSegment] = []
    issues: List[Issue] = []

    def add_issue(spec: SegmentSpec, code: str, description: str) -> None:
        issues.append(
            Issue(
                manifest_index=spec.manifest_index,
                path=spec.path,
                code=code,
                description=description,
            )
        )

    for spec in specs:
        canonical_path = os.path.normcase(os.path.abspath(spec.resolved_path))
        key = (canonical_path, spec.start)
        if key in seen_keys:
            add_issue(
                spec,
                "duplicate",
                f"duplicate of manifest entry {seen_keys[key]} "
                "(same path and start); segment skipped",
            )
            continue
        seen_keys[key] = spec.manifest_index

        try:
            file_exists = os.path.isfile(spec.resolved_path)
        except (OSError, ValueError):
            file_exists = False
        if not file_exists:
            add_issue(spec, "missing", "file not found; segment skipped")
            continue

        try:
            samples = read_segment(spec.resolved_path)
        except AudioFormatError as exc:
            add_issue(spec, "invalid_format", f"{exc}; segment skipped")
            continue

        try:
            fade_in_ms, fade_in_samples = _fade_from_raw(
                spec.fade_in_raw, "fade_in_ms"
            )
            fade_out_ms, fade_out_samples = _fade_from_raw(
                spec.fade_out_raw, "fade_out_ms"
            )
        except FadeError as exc:
            add_issue(spec, "invalid_fade", f"invalid fade: {exc}; segment skipped")
            continue

        frame_count = len(samples)
        if fade_in_samples * 2 > frame_count or fade_out_samples * 2 > frame_count:
            add_issue(
                spec,
                "fade_too_long",
                "fade too long: "
                f"fade-in {_format_decimal(fade_in_ms)} ms, "
                f"fade-out {_format_decimal(fade_out_ms)} ms, "
                f"segment duration {_format_decimal(Decimal(frame_count) / SAMPLES_PER_MILLISECOND)} ms "
                f"(half = {frame_count / 2:g} samples); segment skipped",
            )
            continue

        start_sample = spec.start_sample
        end_sample = start_sample + frame_count

        conflict: Optional[AcceptedSegment] = None
        for other in accepted:
            if _intervals_overlap(
                start_sample, end_sample, other.start_sample, other.end_sample
            ):
                conflict = other
                break
        if conflict is not None:
            add_issue(
                spec,
                "overlap",
                f"overlap with manifest entry {conflict.spec.manifest_index} "
                f"({conflict.spec.path}); segment skipped",
            )
            continue

        _apply_fades(samples, fade_in_samples, fade_out_samples)
        accepted.append(
            AcceptedSegment(
                spec=spec,
                start_sample=start_sample,
                end_sample=end_sample,
                samples=samples,
                fade_in_ms=fade_in_ms,
                fade_out_ms=fade_out_ms,
                fade_in_samples=fade_in_samples,
                fade_out_samples=fade_out_samples,
            )
        )

    ordered = sorted(accepted, key=lambda item: (item.start_sample, item.spec.manifest_index))
    total_samples = max((item.end_sample for item in ordered), default=0)

    silences: List[Tuple[int, int]] = []
    cursor = 0
    for item in ordered:
        if item.start_sample > cursor:
            silences.append((cursor, item.start_sample))
        cursor = item.end_sample

    return AssemblyResult(
        segments=ordered,
        issues=issues,
        silences=silences,
        total_samples=total_samples,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def render_episode(result: AssemblyResult) -> array.array:
    """Create the output sample buffer with zero-filled gaps."""
    output = array.array("h", bytes(result.total_samples * SAMPLE_WIDTH_BYTES))
    for segment in result.segments:
        output[segment.start_sample:segment.end_sample] = segment.samples
    return output


def build_report(
    result: AssemblyResult,
    manifest_path: os.PathLike | str,
    output_path: os.PathLike | str,
) -> Dict[str, Any]:
    successful = []
    for segment in result.segments:
        successful.append(
            {
                "manifest_index": segment.spec.manifest_index,
                "path": segment.spec.path,
                "start_seconds": samples_to_seconds(segment.start_sample),
                "end_seconds": samples_to_seconds(segment.end_sample),
                "fade": bool(segment.fade_in_samples or segment.fade_out_samples),
                "fade_in_ms": float(segment.fade_in_ms),
                "fade_out_ms": float(segment.fade_out_ms),
            }
        )

    return {
        "manifest": os.fspath(manifest_path),
        "output": os.fspath(output_path),
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "sample_width_bits": SAMPLE_WIDTH_BITS,
        "duration_seconds": samples_to_seconds(result.total_samples),
        "successful_segments": successful,
        "issues": [
            {
                "manifest_index": issue.manifest_index,
                "path": issue.path,
                "code": issue.code,
                "description": issue.description,
            }
            for issue in result.issues
        ],
        "silence_intervals": [
            {
                "start_seconds": samples_to_seconds(start),
                "end_seconds": samples_to_seconds(end),
            }
            for start, end in result.silences
        ],
    }


def _ensure_parent(path: os.PathLike | str) -> None:
    parent = os.path.dirname(os.path.abspath(os.fspath(path)))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_episode(path: os.PathLike | str, samples: array.array) -> None:
    _ensure_parent(path)
    payload = samples
    if sys.byteorder != "little":
        payload = array.array("h", samples)
        payload.byteswap()
    with wave.open(os.fspath(path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(payload.tobytes())


def write_report(path: os.PathLike | str, report: Dict[str, Any]) -> None:
    _ensure_parent(path)
    with open(os.fspath(path), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podcast_assembler",
        description=(
            "Assemble a 16-bit PCM mono 44.1 kHz podcast episode from WAV "
            "segments listed in a JSON manifest."
        ),
    )
    parser.add_argument("manifest", help="path to the JSON manifest")
    parser.add_argument(
        "-o",
        "--output",
        default="episode.wav",
        help="output WAV path (default: episode.wav)",
    )
    parser.add_argument(
        "-r",
        "--report",
        default="report.json",
        help="output JSON report path (default: report.json)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        specs = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"error: invalid manifest: {exc}", file=sys.stderr)
        return 2

    result = assemble(specs)
    report = build_report(result, args.manifest, args.output)

    try:
        write_episode(args.output, render_episode(result))
        write_report(args.report, report)
    except OSError as exc:
        print(f"error: cannot write output: {exc}", file=sys.stderr)
        return 1

    duration = report["duration_seconds"]
    print(
        f"wrote {len(result.segments)} segment(s) to {args.output} "
        f"({duration:.3f} s); report: {args.report}"
    )

    issue_count = len(result.issues)
    if issue_count:
        noun = "issue" if issue_count == 1 else "issues"
        print(
            f"podcast: {issue_count} {noun} - see {args.report}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
