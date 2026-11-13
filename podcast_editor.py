#!/usr/bin/env python3
"""Podcast post-production tool.

Reads a mono 16-bit PCM WAV master, cuts clips from a JSON manifest,
normalizes each clip to a target peak level, merges the successful
non-silent clips in manifest order, and writes a JSON report.

Standard library only. Python 3.10+.

Exit codes
----------
0  success
2  invalid command line arguments
3  input WAV rejected (corrupt header / wrong encoding)
4  manifest file missing or not valid JSON
5  a clip write failed mid-run (external report marks manual intervention)
6  report itself could not be written
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any

# ---- Constants -----------------------------------------------------------

FULL_SCALE = 32767.0  # max amplitude of signed 16-bit PCM
MIN_PEAK_DB = -30.0
MAX_PEAK_DB = 0.0
DEFAULT_PEAK_DB = -1.0
DEFAULT_MERGED_NAME = "merged.wav"
DEFAULT_REPORT_NAME = "report.json"
WAVE_EXT = ".wav"


class InputRejectedError(Exception):
    """The input WAV cannot be accepted (header/encoding mismatch)."""


# ---- Small helpers -------------------------------------------------------


def target_amplitude(target_db: float) -> int:
    """Convert a dBFS target to a 16-bit integer amplitude."""
    return int(round(FULL_SCALE * (10.0 ** (target_db / 20.0))))


def peak_dbfs(samples: array.array) -> float | None:
    """Peak level in dBFS; None for an all-zero (silent) clip."""
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return None
    return 20.0 * math.log10(peak / FULL_SCALE)


def ms_to_sample(ms: int, sample_rate: int) -> int:
    """Round half up so e.g. 1 ms at 8 kHz maps to 8 samples deterministically."""
    return math.floor(ms * sample_rate / 1000.0 + 0.5)


def sample_to_ms(n_samples: int, sample_rate: int) -> int:
    return math.floor(n_samples * 1000.0 / sample_rate + 0.5)


# ---- WAV reading / writing ----------------------------------------------


def read_mono_pcm16_wav(path: str) -> tuple[int, array.array]:
    """Validate and read a mono 16-bit PCM WAV.

    Returns (sample_rate, samples). Raises InputRejectedError for any
    header/encoding problem so the caller can reject the whole run
    before producing output.
    """
    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            compression = wf.getcomptype()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise InputRejectedError(f"cannot read WAV header/frames: {exc}") from exc

    if compression != "NONE":
        raise InputRejectedError(
            f"unsupported compression {compression!r}; only uncompressed PCM is accepted"
        )
    if n_channels != 1:
        raise InputRejectedError(
            f"expected mono (1 channel), got {n_channels} channels"
        )
    if sample_width != 2:
        raise InputRejectedError(
            f"expected 16-bit PCM (sample width 2 bytes), got {sample_width * 8}-bit"
        )
    if not (8000 <= sample_rate <= 48000):
        raise InputRejectedError(
            f"sample rate {sample_rate} Hz outside accepted range 8000-48000 Hz"
        )

    expected_bytes = n_frames * 2
    if len(raw) != expected_bytes:
        raise InputRejectedError(
            f"truncated data: expected {expected_bytes} PCM bytes, got {len(raw)}"
        )
    samples = array.array("h")
    try:
        samples.frombytes(raw)
    except (ValueError, EOFError) as exc:
        raise InputRejectedError(f"corrupt PCM payload: {exc}") from exc
    return sample_rate, samples


def write_pcm16_wav(path: str, sample_rate: int, samples: array.array) -> None:
    """Write a mono 16-bit PCM WAV (non-atomic, used inside atomic writer)."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())


def write_pcm16_wav_atomic(path: str, sample_rate: int, samples: array.array) -> None:
    """Write via a temp file in the same directory, then replace the target.

    A failure mid-write leaves any previously existing target untouched and
    never leaves a half-written file at the final path.
    """
    target = Path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                    dir=str(target.parent))
    os.close(fd)
    try:
        write_pcm16_wav(tmp_name, sample_rate, samples)
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


# ---- Manifest validation -------------------------------------------------


def load_manifest(path: str) -> list[dict[str, Any]]:
    """Load the manifest. Top-level structure problems are fatal (exit 4)."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError("manifest root must be a JSON array of clip entries")
    if not all(isinstance(item, dict) for item in data):
        raise ValueError("every manifest entry must be a JSON object")
    return data


def validate_entry(entry: dict[str, Any]) -> tuple[int, int] | str:
    """Return (start_ms, end_ms) for a valid entry, otherwise a reason string."""
    for field in ("start_ms", "end_ms"):
        if field not in entry:
            return f"missing required field {field!r}"
        # bool is a subclass of int: reject it explicitly as an illegal time.
        if not isinstance(entry[field], int) or isinstance(entry[field], bool):
            return f"field {field!r} must be an integer number of milliseconds"
    start_ms, end_ms = entry["start_ms"], entry["end_ms"]
    if start_ms < 0:
        return f"start_ms must be >= 0, got {start_ms}"
    if end_ms <= start_ms:
        return f"end_ms ({end_ms}) must be greater than start_ms ({start_ms})"
    return start_ms, end_ms


def resolve_output_name(entry: dict[str, Any], index: int) -> tuple[str, str | None]:
    """Sanitize an optional output_name; auto-generate one when absent/invalid."""
    raw = entry.get("output_name")
    if raw is None:
        return f"clip_{index:03d}{WAVE_EXT}", None
    if not isinstance(raw, str) or not raw.strip():
        return f"clip_{index:03d}{WAVE_EXT}", (
            "output_name was not a non-empty string; auto-generated file name used"
        )
    name = raw.strip().replace("\\", "_").replace("/", "_")
    if not name.lower().endswith(WAVE_EXT):
        name += WAVE_EXT
    return name, None


# ---- Peak adjustment -----------------------------------------------------


def adjust_peak(samples: array.array, target_db: float) -> tuple[array.array, int, int]:
    """Scale samples so the peak lands on target_db (negative gain or boost).

    Returns (adjusted_samples, original_peak, adjusted_peak).
    All-zero input is returned unchanged with both peaks at 0 (silence).
    """
    original_peak = max((abs(s) for s in samples), default=0)
    if original_peak == 0:
        return array.array("h", samples), 0, 0
    target_peak = target_amplitude(target_db)
    factor = target_peak / original_peak

    def clamp(v: float) -> int:
        if v > 32767:
            return 32767
        if v < -32768:
            return -32768
        return int(math.floor(v + 0.5))

    adjusted = array.array("h", (clamp(s * factor) for s in samples))
    new_peak = max(abs(s) for s in adjusted)
    return adjusted, original_peak, new_peak


# ---- Report --------------------------------------------------------------


def write_report(report: dict[str, Any], report_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---- Core processing -----------------------------------------------------


def run(input_wav: str, manifest_path: str, output_dir: str,
        target_db: float, merged_name: str, report_path: str) -> dict[str, Any]:
    sample_rate, samples = read_mono_pcm16_wav(input_wav)
    total_samples = len(samples)
    total_ms = sample_to_ms(total_samples, sample_rate)
    target_peak = target_amplitude(target_db)

    manifest = load_manifest(manifest_path)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "input_wav": os.path.abspath(input_wav),
        "sample_rate_hz": sample_rate,
        "total_duration_ms": total_ms,
        "target_peak_dbfs": target_db,
        "target_peak_amplitude": target_peak,
        "merged_file": None,
        "success": [],
        "skipped": [],
        "silence": [],
        "warnings": [],
        "manual_intervention_required": False,
        "failure": None,
    }

    merged_parts: list[array.array] = []
    expected_merged_ms = 0

    for index, entry in enumerate(manifest):
        entry_label = f"entry #{index}"

        def skip(reason: str) -> None:
            report["skipped"].append(
                {"entry_index": index, "reason": reason, "entry": _safe_entry(entry)}
            )

        checked = validate_entry(entry)
        if isinstance(checked, str):
            skip(checked)
            continue
        start_ms, end_ms = checked

        start_sample = ms_to_sample(start_ms, sample_rate)
        if start_sample >= total_samples:
            skip(
                f"start_ms {start_ms} (sample {start_sample}) is at or beyond "
                f"the audio end ({total_ms} ms / {total_samples} samples)"
            )
            continue

        end_sample = min(ms_to_sample(end_ms, sample_rate), total_samples)
        if end_sample <= start_sample:
            skip(
                f"clamped interval [sample {start_sample}, {end_sample}) is empty"
            )
            continue

        if end_sample < ms_to_sample(end_ms, sample_rate):
            report["warnings"].append(
                f"{entry_label}: end_ms {end_ms} exceeded audio end; "
                f"clipped to {sample_to_ms(end_sample, sample_rate)} ms"
            )

        file_name, name_warning = resolve_output_name(entry, index)
        if name_warning:
            report["warnings"].append(f"{entry_label}: {name_warning}")
        clip_path = str(out_dir / file_name)

        clip = array.array("h", samples[start_sample:end_sample])
        adjusted, original_peak, adjusted_peak = adjust_peak(clip, target_db)
        is_silent = original_peak == 0
        duration_ms = sample_to_ms(len(adjusted), sample_rate)

        overwritten = os.path.exists(clip_path)
        try:
            write_pcm16_wav_atomic(clip_path, sample_rate, adjusted)
        except OSError as exc:
            # Mid-run write failure: stop, keep completed clips, ask for help.
            report["failure"] = (
                f"clip write failed for {entry_label} ({file_name}): {exc}. "
                "Processing stopped; completed clips remain readable."
            )
            report["manual_intervention_required"] = True
            write_report(report, report_path)
            return report

        entry_info = {
            "entry_index": index,
            "output_name": file_name,
            "duration_ms": duration_ms,
            "original_peak_amplitude": original_peak,
            "adjusted_peak_amplitude": adjusted_peak,
            "overwritten": overwritten,
        }
        if overwritten:
            msg = f"{entry_label}: existing file {file_name} was overwritten"
            report["warnings"].append(msg)
            entry_info["overwrite_warning"] = msg

        if is_silent:
            info = dict(entry_info)
            info["note"] = "all-zero clip; level adjustment skipped; excluded from merge"
            report["silence"].append(info)
            continue

        report["success"].append(entry_info)
        merged_parts.append(adjusted)
        expected_merged_ms += duration_ms

    # Merge successful, non-silent clips in manifest order.
    if merged_parts:
        merged_samples = array.array("h")
        for part in merged_parts:
            merged_samples.extend(part)
        merged_path = str(out_dir / merged_name)
        if os.path.exists(merged_path):
            report["warnings"].append(
                f"existing merged file {merged_name} was overwritten"
            )
        write_pcm16_wav_atomic(merged_path, sample_rate, merged_samples)
        report["merged_file"] = os.path.abspath(merged_path)
        report["merged_duration_ms"] = sample_to_ms(len(merged_samples), sample_rate)
    else:
        report["merged_duration_ms"] = 0

    write_report(report, report_path)
    return report


def _safe_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of an entry for the report (keeps primitive values)."""
    return {k: v for k, v in entry.items() if isinstance(v, (str, int, float, bool))}


# ---- Command line --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cut, peak-normalize and merge WAV clips from a JSON manifest."
    )
    parser.add_argument("input_wav", help="mono 16-bit PCM WAV master file")
    parser.add_argument("manifest", help="JSON clip manifest (array of entries)")
    parser.add_argument("output_dir", help="directory for clips, merged file and report")
    parser.add_argument(
        "-p", "--target-peak",
        default=str(DEFAULT_PEAK_DB),
        help=f"target peak in dBFS, {MIN_PEAK_DB:g}..{MAX_PEAK_DB:g} "
             f"(default {DEFAULT_PEAK_DB:g})",
    )
    parser.add_argument(
        "-m", "--merged-name", default=DEFAULT_MERGED_NAME,
        help=f"merged WAV file name inside output_dir (default {DEFAULT_MERGED_NAME})",
    )
    parser.add_argument(
        "-r", "--report-path", default=None,
        help="report path (default: <output_dir>/%s)" % DEFAULT_REPORT_NAME,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        target_db = float(args.target_peak)
    except (TypeError, ValueError):
        parser.error(f"target peak must be numeric, got {args.target_peak!r}")
    if not (MIN_PEAK_DB <= target_db <= MAX_PEAK_DB):
        parser.error(
            f"target peak {target_db:g} dBFS out of range "
            f"[{MIN_PEAK_DB:g}, {MAX_PEAK_DB:g}]"
        )

    report_path = args.report_path or os.path.join(args.output_dir, DEFAULT_REPORT_NAME)

    try:
        report = run(args.input_wav, args.manifest, args.output_dir,
                     target_db, args.merged_name, report_path)
    except InputRejectedError as exc:
        print(f"ERROR: input WAV rejected: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"ERROR: manifest not found: {exc}", file=sys.stderr)
        return 4
    except json.JSONDecodeError as exc:
        print(f"ERROR: manifest is not valid JSON: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"ERROR: invalid manifest: {exc}", file=sys.stderr)
        return 4
    except OSError as exc:
        print(f"ERROR: I/O failure: {exc}", file=sys.stderr)
        return 5

    if report.get("manual_intervention_required"):
        print(
            "FAILED: a clip could not be written; completed clips kept; "
            f"report at {os.path.abspath(report_path)}",
            file=sys.stderr,
        )
        return 5
    try:
        # Report was written inside run(); double-check it is on disk.
        if not os.path.exists(report_path):
            write_report(report, report_path)
    except OSError as exc:
        print(f"ERROR: failed to write report: {exc}", file=sys.stderr)
        return 6

    print(
        f"OK: {len(report['success'])} clip(s), "
        f"{len(report['silence'])} silent, {len(report['skipped'])} skipped, "
        f"{len(report['warnings'])} warning(s); report: {os.path.abspath(report_path)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
