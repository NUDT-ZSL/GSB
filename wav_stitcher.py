#!/usr/bin/env python3
"""
PCM WAV clip stitcher with per-clip gain normalisation.

Reads a list of 16-bit PCM WAV clips, validates them, crops each clip to a
requested time window, normalises each clip's RMS to a target loudness
(gain clamped to +/-20 dB and peak-limited to <= 0 dBFS), then concatenates
the surviving clips in input order.  A JSON report describing every input
is always written, even when no clip can be used.

Only the Python standard library is used (Python 3.10+).

Usage example:
    python wav_stitcher.py \
        --inputs a.wav b.wav c.wav \
        --crops full 0.2:0.6 2:1 \
        --out-wav out/stitched.wav \
        --report out/report.json \
        --target-loudness -20

Exit codes:
    0  at least one clip was stitched and the WAV was written
    1  no clip could be used (report only, no WAV)
    2  invalid command line arguments / unusable output location
    3  WAV writing failed after clips were processed
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import re
import sys
import tempfile
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

TOOL_NAME = "pcm-wav-stitcher"
TOOL_VERSION = "1.0.0"

REQUIRED_SAMPLE_WIDTH = 2          # bytes == 16-bit PCM
FULL_SCALE = 32768.0               # int16 full scale for dBFS
DEFAULT_TARGET_DBFS = -20.0
TARGET_RANGE_MIN_DBFS = -60.0
TARGET_RANGE_MAX_DBFS = 0.0
GAIN_LIMIT_DB = 20.0
MAX_INT16 = 32767
MIN_INT16 = -32768
REPORT_DECIMALS = 2


# --------------------------------------------------------------------------- #
# Errors and small helpers
# --------------------------------------------------------------------------- #
class SegmentError(Exception):
    """A single clip could not be used; message becomes the report reason."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _round2(value: float) -> float:
    """Round to 2 decimals, normalising negative zero."""
    rounded = round(float(value), REPORT_DECIMALS)
    if rounded == 0:
        return 0.0
    return rounded


def metric(value: float | None, unit: str, note: str | None = None) -> dict[str, Any]:
    """A numeric measurement together with a 2-decimal display string and unit."""
    if value is None or not math.isfinite(value):
        label = "-inf" if unit == "dBFS" else "n/a"
        item: dict[str, Any] = {
            "value": None,
            "formatted": f"{label} {unit}",
            "unit": unit,
        }
        if note:
            item["note"] = note
        return item

    rounded = _round2(value)
    item = {
        "value": rounded,
        "formatted": f"{rounded:.2f} {unit}",
        "unit": unit,
    }
    if note:
        item["note"] = note
    return item


def _seconds_metric(value: float | None, note: str | None = None) -> dict[str, Any]:
    return metric(value, "s", note)


def dbfs_from_rms(rms: float) -> float | None:
    """RMS amplitude (int16 scale) -> dBFS. Silence has no finite dBFS value."""
    if rms <= 0.0:
        return None
    return 20.0 * math.log10(rms / FULL_SCALE)


def dbfs_from_peak(peak_abs: int) -> float | None:
    if peak_abs <= 0:
        return None
    return 20.0 * math.log10(peak_abs / FULL_SCALE)


def _bytes_to_samples(data: bytes) -> array.array:
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _samples_to_bytes(samples: array.array) -> bytes:
    if sys.byteorder == "big":
        copy = array.array("h", samples)
        copy.byteswap()
        return copy.tobytes()
    return samples.tobytes()


def _rms(samples: Sequence[int]) -> float | None:
    if not samples:
        return None
    total = 0
    for sample in samples:
        total += sample * sample
    return math.sqrt(total / len(samples))


def _peak(samples: Sequence[int]) -> int:
    return max((abs(sample) for sample in samples), default=0)


def _format_range(start: float, end: float) -> str:
    return f"{_round2(start):.2f} - {_round2(end):.2f} s"


# --------------------------------------------------------------------------- #
# WAV loading / validation
# --------------------------------------------------------------------------- #
@dataclass
class LoadedClip:
    path: str
    name: str
    rate: int
    channels: int
    nframes: int
    data: bytes

    @property
    def frame_size(self) -> int:
        return self.channels * REQUIRED_SAMPLE_WIDTH

    @property
    def duration(self) -> float:
        return self.nframes / self.rate


def load_wav(path: str) -> LoadedClip:
    """Parse a 16-bit PCM WAV file or raise SegmentError with a precise reason."""
    file_path = Path(path)
    if not file_path.exists():
        raise SegmentError("file not found")
    if not file_path.is_file():
        raise SegmentError("path is not a regular file")
    if file_path.stat().st_size == 0:
        raise SegmentError("file is empty")

    try:
        with wave.open(str(file_path), "rb") as wav:
            if wav.getcomptype() != "NONE":
                raise SegmentError(
                    f"unsupported compression type {wav.getcomptype()!r}; "
                    "only uncompressed PCM is supported"
                )
            sample_width = wav.getsampwidth()
            if sample_width != REQUIRED_SAMPLE_WIDTH:
                raise SegmentError(
                    f"unsupported bit depth {sample_width * 8}-bit; "
                    "only 16-bit PCM is supported"
                )
            channels = wav.getnchannels()
            rate = wav.getframerate()
            nframes = wav.getnframes()
            if channels < 1 or rate < 1:
                raise SegmentError(
                    f"invalid stream parameters (channels={channels}, rate={rate})"
                )
            frame_size = channels * sample_width
            expected_bytes = nframes * frame_size
            data = wav.readframes(nframes)
    except SegmentError:
        raise
    except (wave.Error, EOFError) as exc:
        detail = str(exc).strip() or (
            f"file is only {file_path.stat().st_size} bytes, too short to "
            "contain a valid WAV header"
        )
        raise SegmentError(f"invalid or truncated WAV header/data: {detail}") from exc
    except OSError as exc:
        raise SegmentError(f"cannot read file: {exc}") from exc

    if len(data) != expected_bytes:
        raise SegmentError(
            "truncated audio data: header declares "
            f"{nframes} frames ({expected_bytes} bytes), "
            f"but only {len(data)} bytes were readable"
        )

    return LoadedClip(
        path=str(path),
        name=file_path.name,
        rate=rate,
        channels=channels,
        nframes=nframes,
        data=data,
    )


# --------------------------------------------------------------------------- #
# Target loudness / crop specs
# --------------------------------------------------------------------------- #
def resolve_target_loudness(requested: float | None) -> tuple[float, str | None]:
    """Fall back to -20 dBFS with a warning when the requested target is invalid."""
    default = DEFAULT_TARGET_DBFS
    if requested is None:
        warning = (
            "WARNING: no target loudness given; "
            f"using default {default:.2f} dBFS."
        )
        return default, warning

    if not math.isfinite(requested) or not (
        TARGET_RANGE_MIN_DBFS <= requested <= TARGET_RANGE_MAX_DBFS
    ):
        warning = (
            f"WARNING: target loudness {requested!r} dBFS is outside the allowed "
            f"range [{TARGET_RANGE_MIN_DBFS:.0f}, {TARGET_RANGE_MAX_DBFS:.0f}] dBFS; "
            f"falling back to default {default:.2f} dBFS."
        )
        return default, warning

    return float(requested), None


_CROP_RE = re.compile(
    r"^\s*(?P<start>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*:\s*"
    r"(?P<end>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)


def parse_crop_token(token: str) -> tuple[float, float] | None:
    """Parse ``START:END`` seconds.  ``full`` / ``none`` / ``auto`` mean no crop."""
    if token is None:
        return None
    stripped = token.strip()
    if stripped.lower() in {"full", "none", "auto"}:
        return None
    match = _CROP_RE.match(stripped)
    if not match:
        raise ValueError(
            f"invalid crop specification {token!r}; expected START:END (seconds) or 'full'"
        )
    start = float(match.group("start"))
    end = float(match.group("end"))
    if not math.isfinite(start) or not math.isfinite(end):
        raise ValueError(f"crop values must be finite numbers, got {token!r}")
    return start, end


def _crop_details(
    clip: LoadedClip,
    requested: tuple[float, float] | None,
) -> tuple[dict[str, Any], tuple[int, int] | None]:
    """Return (report crop block, (start_frame, end_frame) or None if empty)."""
    requested_start = 0.0 if requested is None else requested[0]
    requested_end = clip.duration if requested is None else requested[1]

    clamped_start = _clamp(requested_start, 0.0, clip.duration)
    clamped_end = _clamp(requested_end, 0.0, clip.duration)
    start_frame = int(round(clamped_start * clip.rate))
    end_frame = int(round(clamped_end * clip.rate))
    start_frame = max(0, min(clip.nframes, start_frame))
    end_frame = max(0, min(clip.nframes, end_frame))

    truncated = (
        abs(clamped_start - requested_start) > 1e-9
        or abs(clamped_end - requested_end) > 1e-9
    )
    empty = end_frame <= start_frame

    actual_start = start_frame / clip.rate
    actual_end = end_frame / clip.rate
    duration = max(0, end_frame - start_frame) / clip.rate

    crop_block: dict[str, Any] = {
        "requested_start": _seconds_metric(requested_start),
        "requested_end": _seconds_metric(requested_end),
        "actual_start": _seconds_metric(actual_start),
        "actual_end": _seconds_metric(actual_end),
        "duration": _seconds_metric(duration if not empty else 0.0),
        "unit": "s",
        "requested_formatted": _format_range(requested_start, requested_end),
        "actual_formatted": _format_range(actual_start, actual_end),
        "truncated_to_clip_bounds": bool(truncated),
    }
    if empty:
        return crop_block, None
    return crop_block, (start_frame, end_frame)


# --------------------------------------------------------------------------- #
# Gain / peak limiting
# --------------------------------------------------------------------------- #
def normalise_clip(
    samples: array.array, target_dbfs: float
) -> tuple[array.array, dict[str, Any]]:
    """Apply target-RMS gain (clamped +/-20 dB, peak limited) to a clip."""
    input_rms = _rms(samples)
    input_dbfs = dbfs_from_rms(input_rms) if input_rms is not None else None
    input_peak = _peak(samples)

    if input_rms is None or input_rms <= 0.0:
        output = array.array("h", bytes(len(samples) * 2))
        final_rms = _rms(output)
        final_peak = _peak(output)
        gain_block = {
            "value": 0.0,
            "formatted": "0.00 dB",
            "unit": "dB",
            "requested_value": None,
            "requested_formatted": "n/a dB",
            "applied_value": 0.0,
            "applied_formatted": "0.00 dB",
            "range_limited": False,
            "peak_limited": False,
            "note": "silent clip (input RMS = 0); gain left at 0 dB",
        }
        return output, {
            "input_rms": metric(input_dbfs, "dBFS", "silent clip"),
            "gain": gain_block,
            "final_rms": metric(
                dbfs_from_rms(final_rms) if final_rms else None,
                "dBFS",
                "silent clip",
            ),
            "final_peak": metric(dbfs_from_peak(final_peak), "dBFS"),
        }

    requested_gain = target_dbfs - input_dbfs
    gain_db = _clamp(requested_gain, -GAIN_LIMIT_DB, GAIN_LIMIT_DB)
    range_limited = abs(gain_db - requested_gain) > 1e-9
    linear = 10.0 ** (gain_db / 20.0)

    peak_limited = False
    applied_linear = linear
    if input_peak > 0 and input_peak * linear > FULL_SCALE:
        # Scale the whole clip down so its highest sample sits at 32767
        # (about -0.0003 dBFS) instead of hard-clipping individual samples.
        applied_linear = MAX_INT16 / input_peak
        peak_limited = True

    output = array.array("h", bytes(len(samples) * 2))
    for index, sample in enumerate(samples):
        value = int(round(sample * applied_linear))
        output[index] = max(MIN_INT16, min(MAX_INT16, value))

    applied_gain_db = (
        20.0 * math.log10(applied_linear) if applied_linear > 0 else None
    )
    applied_block_metric = metric(applied_gain_db, "dB")

    final_rms = _rms(output)
    final_peak = _peak(output)
    gain_block = {
        "value": _round2(gain_db),
        "formatted": f"{_round2(gain_db):.2f} dB",
        "unit": "dB",
        "requested_value": _round2(requested_gain),
        "requested_formatted": f"{_round2(requested_gain):.2f} dB",
        "applied_value": applied_block_metric["value"],
        "applied_formatted": applied_block_metric["formatted"],
        "range_limited": bool(range_limited),
        "peak_limited": bool(peak_limited),
    }
    if peak_limited:
        gain_block["note"] = (
            "gain was peak-limited so the output stays at or below 0 dBFS"
        )
    if range_limited:
        existing_note = gain_block.get("note")
        limit_note = f"requested gain {requested_gain:.2f} dB was clamped to +/-20 dB"
        gain_block["note"] = f"{existing_note}; {limit_note}" if existing_note else limit_note

    return output, {
        "input_rms": metric(input_dbfs, "dBFS"),
        "gain": gain_block,
        "final_rms": metric(
            dbfs_from_rms(final_rms) if final_rms is not None else None,
            "dBFS",
        ),
        "final_peak": metric(dbfs_from_peak(final_peak), "dBFS"),
    }


# --------------------------------------------------------------------------- #
# Stitching pipeline
# --------------------------------------------------------------------------- #
def _segment_record(index: int, path: str) -> dict[str, Any]:
    return {
        "index": index,
        "file_name": Path(path).name,
        "file": path,
        "status": "failed",          # overwritten below
        "participated": False,
        "reason": None,
        "crop": None,
        "input_rms": None,
        "gain": None,
        "final_rms": None,
        "final_peak": None,
        "notes": [],
    }


def _write_wav_atomic(
    path: Path, samples: array.array, rate: int, channels: int
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=".tmp.wav", dir=str(path.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with wave.open(str(temp_path), "wb") as wav:
            wav.setnchannels(channels)
            wav.setsampwidth(REQUIRED_SAMPLE_WIDTH)
            wav.setframerate(rate)
            wav.writeframes(_samples_to_bytes(samples))
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def stitch(
    inputs: Sequence[str],
    out_wav: str,
    report_path: str,
    *,
    target_loudness: float | None = DEFAULT_TARGET_DBFS,
    crops: Sequence[tuple[float, float] | None] | None = None,
    stderr=None,
) -> tuple[int, dict[str, Any]]:
    """Run the full pipeline.  Returns (exit_code, report_dict)."""
    if stderr is None:
        stderr = sys.stderr

    if not inputs:
        raise ValueError("at least one input file is required")

    target_db, target_warning = resolve_target_loudness(target_loudness)
    if target_warning:
        print(target_warning, file=stderr)

    crop_specs: list[tuple[float, float] | None]
    if crops is None:
        crop_specs = [None] * len(inputs)
    else:
        crop_specs = list(crops)
        if len(crop_specs) > len(inputs):
            raise ValueError(
                f"{len(crop_specs)} crop specifications given for only "
                f"{len(inputs)} input files"
            )
        crop_specs.extend([None] * (len(inputs) - len(crop_specs)))

    records: list[dict[str, Any]] = [
        _segment_record(i, path) for i, path in enumerate(inputs)
    ]
    reference: LoadedClip | None = None
    stitched_samples = array.array("h")
    stitched_records: list[dict[str, Any]] = []

    for index, path in enumerate(inputs):
        record = records[index]
        requested_crop = crop_specs[index]

        try:
            clip = load_wav(path)
        except SegmentError as exc:
            record["reason"] = str(exc)
            if requested_crop is not None:
                # No reliable duration is known, so no actual interval is reported.
                record["notes"].append(
                    "clip could not be decoded; crop window was not applied"
                )
            continue
        except Exception as exc:  # defensive: one broken file must not abort the run
            record["reason"] = f"unexpected error while reading file: {exc}"
            continue

        if clip.nframes == 0:
            record["reason"] = "WAV contains no audio frames"
            continue

        if reference is None:
            reference = clip
        else:
            if clip.rate != reference.rate:
                record["reason"] = (
                    f"sample rate {clip.rate} Hz does not match session sample "
                    f"rate {reference.rate} Hz (set by {reference.name})"
                )
                continue
            if clip.channels != reference.channels:
                record["reason"] = (
                    f"channel count {clip.channels} does not match session channel "
                    f"count {reference.channels} (set by {reference.name})"
                )
                continue

        crop_block, frame_window = _crop_details(clip, requested_crop)
        record["crop"] = crop_block

        if frame_window is None:
            record["status"] = "skipped"
            record["reason"] = (
                "crop duration is 0.00 s after clamping to the clip bounds "
                f"(requested {crop_block['requested_formatted']}, "
                f"clip length {clip.duration:.2f} s); "
                "segment not stitched because it has insufficient duration"
            )
            record["notes"].append("segment excluded: zero-length crop window")
            continue

        start_frame, end_frame = frame_window
        start_byte = start_frame * clip.frame_size
        end_byte = end_frame * clip.frame_size
        cropped_samples = _bytes_to_samples(clip.data[start_byte:end_byte])

        if not cropped_samples:
            record["status"] = "skipped"
            record["reason"] = (
                "crop duration is 0.00 s after frame quantisation; "
                "segment not stitched because it has insufficient duration"
            )
            continue

        processed_samples, metrics = normalise_clip(cropped_samples, target_db)
        record.update(metrics)
        record["status"] = "ok"
        record["participated"] = True
        record["reason"] = None
        stitched_samples.extend(processed_samples)
        stitched_records.append(record)

    output_written = False
    output_error: str | None = None
    total_frames = (
        len(stitched_samples) // reference.channels
        if reference is not None
        else 0
    )
    if stitched_records:
        try:
            _write_wav_atomic(
                Path(out_wav), stitched_samples, reference.rate, reference.channels
            )
            output_written = True
        except Exception as exc:  # OSError, wave.Error, ...
            output_error = str(exc)
            print(f"WARNING: could not write output WAV {out_wav}: {exc}", file=stderr)

    output_duration = (
        total_frames / reference.rate
        if output_written and reference is not None
        else None
    )
    output_block = {
        "wav_path": str(out_wav),
        "written": output_written,
        "failure_reason": output_error,
        "sample_rate_hz": reference.rate if reference is not None else None,
        "channels": reference.channels if reference is not None else None,
        "bit_depth": 16,
        "frames": total_frames if output_written else 0,
        "duration": _seconds_metric(output_duration),
    }

    stitched_files = [
        record["file_name"] for record in records if record["participated"]
    ]
    excluded_files = [
        {
            "file_name": record["file_name"],
            "status": record["status"],
            "reason": record["reason"],
        }
        for record in records
        if not record["participated"]
    ]

    requested_metric: dict[str, Any]
    if target_loudness is None or not math.isfinite(float(target_loudness)):
        requested_metric = {
            "value": None,
            "formatted": "n/a dBFS",
            "unit": "dBFS",
        }
    else:
        requested_metric = metric(float(target_loudness), "dBFS")

    report: dict[str, Any] = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "precision_decimals": REPORT_DECIMALS,
        "target_loudness": {
            "requested": requested_metric,
            "applied": metric(target_db, "dBFS"),
            "fallback_to_default": bool(target_warning),
            "default": metric(DEFAULT_TARGET_DBFS, "dBFS"),
            "allowed_range": {
                "min": TARGET_RANGE_MIN_DBFS,
                "max": TARGET_RANGE_MAX_DBFS,
                "unit": "dBFS",
            },
        },
        "warnings": [target_warning] if target_warning else [],
        "session_format": {
            "sample_rate_hz": reference.rate if reference is not None else None,
            "channels": reference.channels if reference is not None else None,
            "bit_depth": 16,
            "reference_file": reference.name if reference is not None else None,
        },
        "input_count": {
            "total": len(inputs),
            "stitched": len(stitched_records),
            "skipped": sum(1 for r in records if r["status"] == "skipped"),
            "failed": sum(1 for r in records if r["status"] == "failed"),
        },
        "stitched_files": stitched_files,
        "excluded_files": excluded_files,
        "output": output_block,
        "segments": records,
    }

    report_file = Path(report_path)
    report_file.parent.mkdir(parents=True, exist_ok=True)
    temp_report = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        prefix=f".{report_file.stem}.",
        suffix=".tmp.json",
        dir=str(report_file.parent),
    )
    try:
        with temp_report:
            json.dump(report, temp_report, ensure_ascii=False, indent=2)
            temp_report.write("\n")
        os.replace(temp_report.name, report_file)
    except Exception:
        try:
            Path(temp_report.name).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    if output_error is not None:
        return 3, report
    return (0 if output_written else 1), report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read_input_list(path: str) -> list[str]:
    list_path = Path(path)
    if not list_path.exists():
        raise ValueError(f"input list file not found: {path}")
    entries: list[str] = []
    for number, line in enumerate(
        list_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(stripped)
    if not entries:
        raise ValueError(f"input list file contains no file paths: {path}")
    return entries


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav_stitcher.py",
        description=(
            "Validate, crop, loudness-normalise and concatenate 16-bit PCM WAV "
            "clips in input order; always emit a JSON report."
        ),
    )
    # argparse treats "-1.5:2" as an option because it starts with '-'; teach
    # it that negative crop windows are values, so out-of-range starts work.
    parser._negative_number_matcher = re.compile(
        r"^-(?:\d+|\d*\.\d+)(?::(?:\d+|\d*\.\d+))?$"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--inputs",
        nargs="+",
        metavar="WAV",
        help="input WAV files, in the order they should be stitched",
    )
    source.add_argument(
        "--input-list",
        metavar="FILE",
        help="text file with one input WAV path per line ('#' starts a comment)",
    )
    parser.add_argument(
        "--crops",
        nargs="+",
        metavar="START:END",
        help=(
            "optional crop windows in seconds, aligned with --inputs order "
            "(fractions allowed, e.g. 0.2:0.6); use 'full' for no crop. "
            "Missing trailing entries default to the full clip."
        ),
    )
    parser.add_argument(
        "--out-wav",
        required=True,
        metavar="PATH",
        help="output WAV path (not created when no clip is usable)",
    )
    parser.add_argument(
        "--report",
        required=True,
        metavar="PATH",
        help="output JSON report path (always created)",
    )
    parser.add_argument(
        "--target-loudness",
        type=float,
        default=DEFAULT_TARGET_DBFS,
        metavar="DBFS",
        help=(
            f"target RMS loudness in dBFS, allowed range "
            f"[{TARGET_RANGE_MIN_DBFS:.0f}, {TARGET_RANGE_MAX_DBFS:.0f}] "
            f"(default: {DEFAULT_TARGET_DBFS:.0f})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.inputs is not None:
            inputs = list(args.inputs)
        else:
            inputs = _read_input_list(args.input_list)

        crop_specs: list[tuple[float, float] | None] | None = None
        if args.crops is not None:
            if len(args.crops) > len(inputs):
                parser.error(
                    f"{len(args.crops)} crop specifications were supplied for "
                    f"only {len(inputs)} input files"
                )
            crop_specs = [parse_crop_token(token) for token in args.crops]

        code, report = stitch(
            inputs,
            args.out_wav,
            args.report,
            target_loudness=args.target_loudness,
            crops=crop_specs,
        )
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable, argparse exits
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    counts = report["input_count"]
    target = report["target_loudness"]["applied"]
    print(f"Target loudness: {target['formatted']}")
    print(
        "Inputs: {total}; stitched: {stitched}; skipped: {skipped}; failed: {failed}".format(
            **counts
        )
    )
    for segment in report["segments"]:
        if segment["status"] == "failed":
            print(
                f"  FAILED  {segment['file_name']}: {segment['reason']}",
                file=sys.stderr,
            )
        elif segment["status"] == "skipped":
            print(
                f"  SKIPPED {segment['file_name']}: {segment['reason']}",
                file=sys.stderr,
            )
    output = report["output"]
    if output["written"]:
        print(
            f"Output WAV: {output['wav_path']} "
            f"({output['duration']['formatted']}, {output['frames']} frames, "
            f"{output['sample_rate_hz']} Hz, {output['channels']} channel(s))"
        )
    else:
        print(f"Output WAV: not written ({output['failure_reason'] or 'no usable clips'})")
    print(f"Report: {args.report}")
    return code


if __name__ == "__main__":
    sys.exit(main())
