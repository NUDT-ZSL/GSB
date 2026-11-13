#!/usr/bin/env python3
"""Deterministic podcast segment concatenator for Windows.

Reads a JSON manifest, validates every referenced mono/16-bit PCM WAV file,
extracts ``[start_ms, end_ms)`` frames, concatenates the usable segments with
silence gaps, peak-normalizes the result and writes ``output.wav`` plus a JSON
report.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

OUTPUT_NAME = "output.wav"
REPORT_NAME = "report.json"
DEFAULT_TARGET_PEAK_DB = -1.0
DEFAULT_GAP_MS = 0


class ManifestError(ValueError):
    """Raised when the manifest as a whole is invalid."""


@dataclass(frozen=True)
class SegmentSpec:
    file: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Manifest:
    target_sample_rate: int
    gap_ms: int
    segments: tuple[SegmentSpec, ...]
    directory: Path


def _is_int(value: Any) -> bool:
    """True for real integers, excluding bool (a subclass of int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require(obj: dict[str, Any], key: str, context: str) -> Any:
    if key not in obj:
        raise ManifestError(f"{context}: missing required field '{key}'")
    return obj[key]


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    """Read and fully validate a manifest before any output is created."""
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest '{manifest_path}': {exc}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError("manifest root must be a JSON object")

    target_rate = _require(raw, "target_sample_rate", "manifest")
    if not _is_int(target_rate) or target_rate <= 0:
        raise ManifestError("target_sample_rate must be a positive integer")

    gap_ms = raw.get("gap_ms", DEFAULT_GAP_MS)
    if not _is_int(gap_ms) or gap_ms < 0:
        raise ManifestError("gap_ms must be a non-negative integer")

    raw_segments = _require(raw, "segments", "manifest")
    if not isinstance(raw_segments, list):
        raise ManifestError("segments must be an array")

    segments: list[SegmentSpec] = []
    for index, item in enumerate(raw_segments):
        context = f"segments[{index}]"
        if not isinstance(item, dict):
            raise ManifestError(f"{context} must be a JSON object")

        file_value = _require(item, "file", context)
        if not isinstance(file_value, str) or not file_value.strip():
            raise ManifestError(f"{context}.file must be a non-empty string")

        start_ms = _require(item, "start_ms", context)
        end_ms = _require(item, "end_ms", context)
        if not _is_int(start_ms):
            raise ManifestError(f"{context}.start_ms must be an integer")
        if not _is_int(end_ms):
            raise ManifestError(f"{context}.end_ms must be an integer")
        if start_ms < 0:
            raise ManifestError(f"{context}.start_ms must be non-negative")
        if end_ms < 0:
            raise ManifestError(f"{context}.end_ms must be non-negative")
        if start_ms >= end_ms:
            raise ManifestError(
                f"{context}: start_ms ({start_ms}) must be less than end_ms ({end_ms})"
            )

        segments.append(SegmentSpec(file=file_value, start_ms=start_ms, end_ms=end_ms))

    return Manifest(
        target_sample_rate=target_rate,
        gap_ms=gap_ms,
        segments=tuple(segments),
        directory=manifest_path.resolve().parent,
    )


def _resolve_media_path(manifest: Manifest, file_name: str) -> Path:
    """Relative media paths are resolved against the manifest directory."""
    path = Path(file_name)
    if not path.is_absolute():
        path = manifest.directory / path
    return path


def _duration_ms(frames: int, sample_rate: int) -> int | float:
    if sample_rate <= 0:
        return 0
    value = frames * 1000.0 / sample_rate
    if math.isclose(value, round(value), abs_tol=1e-9):
        return int(round(value))
    return round(value, 3)


def _read_segment(
    spec: SegmentSpec, path: Path, target_rate: int
) -> tuple[array.array | None, str | None, str | None]:
    """Return (samples, error, warning) for one segment.

    ``samples`` is None exactly when ``error`` is set.  A segment with start
    beyond EOF yields zero samples and a warning, never an error.
    """
    if not path.is_file():
        return None, f"file does not exist: {path}", None

    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()

            if compression != "NONE":
                return None, f"expected uncompressed PCM, got compression '{compression}'", None
            if channels != 1:
                return None, f"expected mono (1 channel), got {channels} channel(s)", None
            if sample_width != 2:
                return None, (
                    f"expected 16-bit PCM (2-byte samples), got "
                    f"{sample_width * 8}-bit samples"
                ), None
            if sample_rate != target_rate:
                return None, (
                    f"sample rate mismatch: expected {target_rate} Hz, "
                    f"got {sample_rate} Hz"
                ), None

            warnings: list[str] = []

            start = spec.start_ms * sample_rate // 1000
            end = spec.end_ms * sample_rate // 1000

            if start >= frame_count:
                warnings.append(
                    f"start_ms {spec.start_ms} is at or beyond file end "
                    f"({_duration_ms(frame_count, sample_rate)} ms); "
                    "no audio was extracted"
                )
                start = frame_count
                end = frame_count
            elif end > frame_count:
                warnings.append(
                    f"end_ms {spec.end_ms} is beyond file end "
                    f"({_duration_ms(frame_count, sample_rate)} ms); "
                    "audio was truncated to the file end"
                )
                end = frame_count

            frames_to_read = max(0, end - start)
            wav.setpos(start)
            raw = wav.readframes(frames_to_read)

        if len(raw) % 2 != 0:
            return None, "corrupt WAV data: odd byte count for 16-bit PCM", None

        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()

        if len(samples) < frames_to_read:
            warnings.append(
                f"expected {frames_to_read} frames but read {len(samples)}; "
                "available audio was kept"
            )

        return samples, None, "; ".join(warnings) if warnings else None
    except (wave.Error, EOFError, OSError, ValueError, struct.error) as exc:
        return None, f"cannot read WAV file: {exc}", None


def _zero_gap(frames: int) -> array.array:
    return array.array("h", [0]) * frames


def _normalize(
    samples: array.array, target_peak_db: float
) -> tuple[array.array, float, int, bool, list[str]]:
    """Peak-normalize all samples by the same gain.

    Returns (output_samples, applied_gain, peak_amplitude, silent, warnings).
    A silent (all-zero) result is left untouched and reported as a warning.
    """
    warnings: list[str] = []
    if len(samples) == 0:
        return array.array("h"), 1.0, 0, False, warnings

    peak = max(abs(value) for value in samples)
    if peak == 0:
        warnings.append(
            "concatenated audio is entirely silent (all samples are zero); "
            "output was kept silent and no gain was applied"
        )
        return array.array("h", samples), 1.0, 0, True, warnings

    target_amplitude = 32767.0 * (10.0 ** (target_peak_db / 20.0))
    gain = target_amplitude / peak

    output = array.array("h")
    append = output.append
    for value in samples:
        scaled = int(round(value * gain))
        if scaled > 32767:
            scaled = 32767
        elif scaled < -32768:
            scaled = -32768
        append(scaled)

    output_peak = max(abs(value) for value in output)
    return output, gain, output_peak, False, warnings


def _samples_to_bytes(samples: array.array) -> bytes:
    if sys.byteorder == "little":
        return samples.tobytes()
    copy = array.array("h", samples)
    copy.byteswap()
    return copy.tobytes()


def _write_wav(path: Path, output_dir: Path, samples: array.array, sample_rate: int) -> None:
    tmp_path = output_dir / (OUTPUT_NAME + ".tmp")
    try:
        with wave.open(str(tmp_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(_samples_to_bytes(samples))
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _write_report(path: Path, report: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def process(
    manifest: Manifest,
    output_dir: str | os.PathLike[str],
    target_peak_db: float = DEFAULT_TARGET_PEAK_DB,
    gap_ms_override: int | None = None,
) -> dict[str, Any]:
    """Process every segment, write output.wav/report.json and return the report."""
    output_directory = Path(output_dir)
    gap_ms = manifest.gap_ms if gap_ms_override is None else gap_ms_override
    sample_rate = manifest.target_sample_rate

    output_directory.mkdir(parents=True, exist_ok=True)

    concatenated = array.array("h")
    segment_reports: list[dict[str, Any]] = []
    success_count = 0
    gap_frames = gap_ms * sample_rate // 1000

    for index, spec in enumerate(manifest.segments):
        media_path = _resolve_media_path(manifest, spec.file)
        samples, error, warning = _read_segment(spec, media_path, sample_rate)

        if error is not None:
            status = "error"
            duration_ms: int | float = 0
        else:
            status = "warning" if warning else "ok"
            duration_ms = _duration_ms(len(samples), sample_rate)
            if success_count > 0:
                concatenated.extend(_zero_gap(gap_frames))
            concatenated.extend(samples)
            success_count += 1

        print(f"Processed {spec.file}: {status}", flush=True)

        segment_reports.append(
            {
                "index": index,
                "file": spec.file,
                "resolved_path": str(media_path.resolve()),
                "start_ms": spec.start_ms,
                "end_ms": spec.end_ms,
                "status": status,
                "error": error,
                "warning": warning,
                "duration_ms": duration_ms,
            }
        )

    normalized, gain, output_peak, silent, report_warnings = _normalize(
        concatenated, target_peak_db
    )

    output_path = (output_directory / OUTPUT_NAME).resolve()
    report_path = (output_directory / REPORT_NAME).resolve()
    _write_wav(output_path, output_directory, normalized, sample_rate)

    if len(normalized) == 0:
        report_warnings.append("output contains no audio frames (zero-length WAV)")

    counts = {"ok": 0, "warning": 0, "error": 0}
    for item in segment_reports:
        counts[item["status"]] += 1

    report: dict[str, Any] = {
        "output_path": str(output_path),
        "report_path": str(report_path),
        "target_sample_rate": sample_rate,
        "gap_ms": gap_ms,
        "target_peak_db": target_peak_db,
        "total_duration_ms": _duration_ms(len(normalized), sample_rate),
        "total_frames": len(normalized),
        "applied_gain": gain,
        "peak_amplitude": output_peak,
        "silent": silent,
        "counts": counts,
        "warnings": report_warnings,
        "segments": segment_reports,
    }
    _write_report(report_path, report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="podcast_concat.py",
        description=(
            "Concatenate mono 16-bit PCM WAV segments from a JSON manifest, "
            "insert silence gaps, peak-normalize and write report.json."
        ),
    )
    parser.add_argument("--manifest", required=True, help="path to the manifest JSON")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="directory for output.wav and report.json (created if missing; default: .)",
    )
    parser.add_argument(
        "--target-peak-db",
        type=float,
        default=DEFAULT_TARGET_PEAK_DB,
        help="peak normalization target in dBFS (default: -1)",
    )
    parser.add_argument(
        "--gap-ms",
        type=int,
        default=None,
        help="silence between usable segments; overrides manifest gap_ms (default: manifest or 0)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.gap_ms is not None and args.gap_ms < 0:
        print("error: --gap-ms must be a non-negative integer", file=sys.stderr)
        return 2
    if not math.isfinite(args.target_peak_db):
        print("error: --target-peak-db must be a finite number", file=sys.stderr)
        return 2

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        process(
            manifest,
            output_dir=args.output_dir,
            target_peak_db=args.target_peak_db,
            gap_ms_override=args.gap_ms,
        )
    except OSError as exc:
        print(f"error: failed to write output: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
