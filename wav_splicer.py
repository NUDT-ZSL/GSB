#!/usr/bin/env python3
"""Podcast WAV splicer: cut 16-bit mono PCM WAV by a millisecond plan and concatenate.

No third-party dependencies.  The implementation deliberately copies PCM bytes
sample-for-sample (no resampling, gain or dithering) and writes the output
atomically so a failed run never leaves a partial production WAV behind.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
REQUIRED_BITS = 16
PCM_FORMAT = 1
BYTES_PER_SAMPLE = 2  # 16-bit mono


# ---------------------------------------------------------------------------
# Errors / report helpers
# ---------------------------------------------------------------------------


def _error(segment_index: int, reason: str) -> dict[str, Any]:
    return {"segment_index": segment_index, "reason": reason}


class PlanError(Exception):
    """The edit plan could not be loaded or has the wrong outer shape."""


# ---------------------------------------------------------------------------
# WAV parsing and validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    sample_frames: int
    data: bytes

    @property
    def duration_ms(self) -> float:
        return self.sample_frames * 1000.0 / self.sample_rate


def _chunk_name(chunk_id: bytes) -> str:
    try:
        return chunk_id.decode("ascii")
    except UnicodeDecodeError:
        return repr(chunk_id)


def parse_wav(raw: bytes) -> tuple[AudioInfo | None, list[str]]:
    """Validate a RIFF/WAVE file and return its PCM payload.

    A non-empty issue list means the file is structurally corrupt, has a
    non-PCM/stereo/non-16-bit layout, or is outside the accepted sample-rate
    range.  ``AudioInfo`` is returned only when the file is fully usable.
    """
    issues: list[str] = []
    file_len = len(raw)

    if file_len < 12:
        return None, [f"file is too small to be a WAV ({file_len} bytes)"]

    riff_id, riff_size, wave_id = struct.unpack_from("<4sI4s", raw, 0)
    if riff_id != b"RIFF":
        return None, [f"missing RIFF header (found {riff_id!r})"]
    if wave_id != b"WAVE":
        return None, [f"RIFF file is not WAVE (found {wave_id!r})"]

    declared_file_len = riff_size + 8
    if riff_size != file_len - 8:
        if declared_file_len > file_len:
            issues.append(
                f"RIFF header declares {declared_file_len} bytes but the file "
                f"contains only {file_len} bytes (truncated)"
            )
        else:
            issues.append(
                f"RIFF header declares {declared_file_len} bytes but the file "
                f"contains {file_len} bytes (chunk lengths do not match the file size)"
            )

    fmt_payload: bytes | None = None
    data_payload: bytes | None = None
    data_declared_size: int | None = None
    pos = 12
    structural_break = False

    while pos < file_len:
        if pos + 8 > file_len:
            issues.append(
                f"incomplete chunk header at offset {pos} "
                f"({file_len - pos} trailing bytes; truncated)"
            )
            structural_break = True
            break

        chunk_id, chunk_size = struct.unpack_from("<4sI", raw, pos)
        payload_start = pos + 8
        payload_end = payload_start + chunk_size

        if payload_end > file_len:
            available = file_len - payload_start
            issues.append(
                f"chunk {_chunk_name(chunk_id)!r} at offset {pos} declares "
                f"{chunk_size} bytes but only {available} bytes "
                f"are available (truncated)"
            )
            if chunk_id == b"fmt " and fmt_payload is None and available >= 16:
                fmt_payload = raw[payload_start:]
            elif chunk_id == b"data" and data_payload is None:
                data_payload = raw[payload_start:]
                data_declared_size = chunk_size
            structural_break = True
            break

        if chunk_id == b"fmt " and fmt_payload is None:
            fmt_payload = raw[payload_start:payload_end]
        elif chunk_id == b"data" and data_payload is None:
            data_payload = raw[payload_start:payload_end]
            data_declared_size = chunk_size

        # RIFF chunks are word aligned: odd-sized chunks are followed by one
        # pad byte which is not counted in the chunk size.
        pos = payload_end + (chunk_size & 1)

    if not structural_break and pos > file_len:
        # An odd final chunk may omit its pad byte; tolerate that only at EOF.
        pos = file_len
    if not structural_break and pos < file_len:
        issues.append(
            f"unexpected trailing bytes after the last chunk "
            f"(declared chunk lengths and file size disagree by {file_len - pos} bytes)"
        )

    if fmt_payload is None:
        issues.append("missing fmt chunk")
    if data_declared_size is None:
        issues.append("missing data chunk")

    if fmt_payload is None:
        return None, issues
    if data_payload is None or data_declared_size is None:
        return None, issues

    if len(fmt_payload) < 16:
        issues.append(
            f"fmt chunk is too small ({len(fmt_payload)} bytes; at least 16 required)"
        )
        return None, issues

    (
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = struct.unpack_from("<HHIIHH", fmt_payload, 0)

    format_issues: list[str] = []
    if audio_format != PCM_FORMAT:
        format_issues.append(
            f"unsupported audio format {audio_format} (must be integer PCM, format 1)"
        )
    if channels != 1:
        stereo_note = "stereo input; " if channels == 2 else ""
        format_issues.append(
            f"unsupported channel count {channels} ({stereo_note}must be mono, 1 channel)"
        )
    if bits_per_sample != REQUIRED_BITS:
        format_issues.append(
            f"unsupported bits_per_sample {bits_per_sample} (must be 16-bit PCM)"
        )
    if not (MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE):
        format_issues.append(
            f"unsupported sample rate {sample_rate} Hz "
            f"(must be between {MIN_SAMPLE_RATE} and {MAX_SAMPLE_RATE} Hz inclusive)"
        )

    expected_block_align = (
        channels * bits_per_sample // 8 if bits_per_sample % 8 == 0 else None
    )
    if expected_block_align is not None and block_align != expected_block_align:
        format_issues.append(
            f"fmt chunk declares block_align={block_align} but channels={channels} "
            f"and bits_per_sample={bits_per_sample} imply {expected_block_align}"
        )
    expected_byte_rate = sample_rate * block_align
    if byte_rate != expected_byte_rate:
        format_issues.append(
            f"fmt chunk declares byte_rate={byte_rate} but sample_rate={sample_rate} "
            f"and block_align={block_align} imply {expected_byte_rate}"
        )

    if block_align <= 0:
        format_issues.append("fmt chunk declares block_align=0 (invalid PCM layout)")
    elif data_declared_size % block_align != 0:
        format_issues.append(
            f"data chunk size {data_declared_size} is not a multiple of "
            f"block_align={block_align} (corrupt or truncated PCM data)"
        )

    # Structural problems (truncated chunks, trailing bytes, missing chunks)
    # and format problems are both fatal; report everything that was found.
    all_issues = issues + format_issues
    if all_issues:
        return None, all_issues

    assert block_align > 0
    sample_frames = data_declared_size // block_align
    return (
        AudioInfo(
            sample_rate=sample_rate,
            channels=channels,
            bits_per_sample=bits_per_sample,
            sample_frames=sample_frames,
            data=data_payload,
        ),
        [],
    )


# ---------------------------------------------------------------------------
# Edit-plan handling
# ---------------------------------------------------------------------------


def load_plan(path: Path) -> list[Any]:
    """Load a plan from either ``[...]`` or ``{"segments": [...]}``."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PlanError(f"cannot read plan file: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PlanError(f"plan file is not valid UTF-8: {exc}") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError(
            f"plan file is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc

    if isinstance(document, dict):
        if "segments" not in document:
            raise PlanError("plan object must contain a 'segments' array")
        segments = document["segments"]
    elif isinstance(document, list):
        segments = document
    else:
        raise PlanError("plan must be a JSON array or an object with a 'segments' array")

    if not isinstance(segments, list):
        raise PlanError("'segments' must be a JSON array")
    return segments


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_segment_shape(index: int, segment: Any) -> tuple[int | None, int | None, str | None]:
    """Return (start_ms, end_ms, error_reason) for one plan entry."""
    if not isinstance(segment, dict):
        return None, None, "segment must be a JSON object with integer start_ms and end_ms"

    problems: list[str] = []
    for field in ("start_ms", "end_ms"):
        if field not in segment:
            problems.append(f"{field} is missing")
        elif not _is_int(segment[field]):
            problems.append(f"{field} must be an integer (got {segment[field]!r})")

    if problems:
        return None, None, "; ".join(problems)
    return segment["start_ms"], segment["end_ms"], None


def segment_range_problem(
    start_ms: int,
    end_ms: int,
    sample_rate: int,
    total_frames: int,
) -> str | None:
    """Return a readable range error, or None when 0 <= start < end <= duration.

    Duration comparisons use integer arithmetic (end_ms * sample_rate <=
    total_frames * 1000) so fractional-millisecond durations cannot be
    misjudged by floating point rounding.
    """
    duration_ms = total_frames * 1000.0 / sample_rate
    problems: list[str] = []

    if start_ms < 0:
        problems.append(f"start_ms must be >= 0 (got {start_ms})")
    if end_ms < 0:
        problems.append(f"end_ms must be >= 0 (got {end_ms})")
    if start_ms >= end_ms:
        problems.append(
            f"start_ms must be less than end_ms (got start_ms={start_ms}, end_ms={end_ms})"
        )

    limit = total_frames * 1000
    if start_ms * sample_rate > limit:
        problems.append(
            f"start_ms={start_ms} is beyond the audio duration of {duration_ms:.3f} ms"
        )
    if end_ms * sample_rate > limit:
        problems.append(
            f"end_ms={end_ms} is beyond the audio duration of {duration_ms:.3f} ms"
        )

    return "; ".join(problems) if problems else None


def ms_to_frame(ms: int, sample_rate: int) -> int:
    """Convert milliseconds to a sample index, rounding to the nearest frame.

    Exact whenever ``ms * sample_rate`` is divisible by 1000 (all edit points
    at 8 kHz and 48 kHz, the accepted range endpoints).
    """
    return (ms * sample_rate + 500) // 1000


# ---------------------------------------------------------------------------
# Output construction / atomic file writing
# ---------------------------------------------------------------------------


def build_wav(info: AudioInfo, segments: Iterable[tuple[int, int]]) -> tuple[bytes, int]:
    """Concatenate independent source slices and return (wav_bytes, total_frames)."""
    parts: list[bytes] = []
    total_frames = 0
    for start_ms, end_ms in segments:
        first = ms_to_frame(start_ms, info.sample_rate)
        limit = ms_to_frame(end_ms, info.sample_rate)
        if not (0 <= first < limit <= info.sample_frames):
            # Validation should already have caught this; guard the invariant.
            raise ValueError(f"invalid frame range {first}:{limit}")
        parts.append(info.data[first * 2 : limit * 2])
        total_frames += limit - first

    payload = b"".join(parts)
    if len(payload) % 2 != 0:  # pragma: no cover - 16-bit frames are always even
        raise ValueError("internal error: odd PCM payload size")

    if len(payload) + 36 > 0xFFFFFFFF:
        raise ValueError(
            "output is too large for the RIFF 32-bit size field "
            f"({len(payload) + 36} bytes)"
        )

    fmt_payload = struct.pack(
        "<HHIIHH",
        PCM_FORMAT,
        1,
        info.sample_rate,
        info.sample_rate * BYTES_PER_SAMPLE,
        BYTES_PER_SAMPLE,
        REQUIRED_BITS,
    )
    header = (
        struct.pack("<4sI4s", b"RIFF", 36 + len(payload), b"WAVE")
        + struct.pack("<4sI", b"fmt ", len(fmt_payload))
        + fmt_payload
        + struct.pack("<4sI", b"data", len(payload))
    )
    return header + payload, total_frames


def atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to a temporary sibling and atomically replace ``path``.

    If anything fails, the temporary file is removed and an pre-existing
    destination is left untouched.  A reader therefore never observes a
    half-written WAV or report.
    """
    parent = path.parent if str(path.parent) else Path(".")
    if not parent.exists():
        raise FileNotFoundError(f"parent directory does not exist: {parent}")
    if not parent.is_dir():
        raise NotADirectoryError(f"parent path is not a directory: {parent}")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav_splicer",
        description=(
            "Cut segments from a 16-bit mono PCM WAV according to a millisecond "
            "edit plan, concatenate them in plan order, and write a JSON report."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, metavar="PATH", help="source WAV file")
    parser.add_argument("--plan", required=True, metavar="PATH", help="JSON edit plan")
    parser.add_argument("--output", required=True, metavar="PATH", help="output WAV path")
    parser.add_argument("--report", required=True, metavar="PATH", help="JSON report path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace the output WAV if it already exists (the report is always rewritten)",
    )
    return parser


def report_bytes(report: dict[str, Any]) -> bytes:
    return (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")


def _same_path(left: Path, right: Path) -> bool:
    """Compare paths the way the local filesystem does (case-insensitive on Windows)."""
    try:
        left_key = os.path.normcase(str(left.resolve()))
        right_key = os.path.normcase(str(right.resolve()))
    except OSError:
        left_key = os.path.normcase(str(left.absolute()))
        right_key = os.path.normcase(str(right.absolute()))
    return left_key == right_key


def _conflicting_paths(
    input_path: Path, plan_path: Path, output_path: Path, report_path: Path
) -> str | None:
    """Reject jobs where a written file would overwrite an input or each other."""
    inputs = {"input": input_path, "plan": plan_path}
    for write_name, write_path in (("output", output_path), ("report", report_path)):
        for source_name, source_path in inputs.items():
            if _same_path(write_path, source_path):
                return (
                    f"--{write_name} and --{source_name} resolve to the same path "
                    f"({write_path}); refusing to overwrite one with the other"
                )
    if _same_path(output_path, report_path):
        return (
            f"--output and --report resolve to the same path ({output_path}); "
            "refusing to overwrite one with the other"
        )
    return None


def _print_errors(errors: list[dict[str, Any]]) -> None:
    for item in errors:
        index = item["segment_index"]
        where = "audio/plan" if index < 0 else f"segment {index}"
        print(f"error [{where}]: {item['reason']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_path = Path(args.input)
    plan_path = Path(args.plan)
    output_path = Path(args.output)
    report_path = Path(args.report)

    # Never let a write target alias an input or the other output.  This is a
    # structural misuse of the CLI, so reject it before touching any file.
    conflict = _conflicting_paths(input_path, plan_path, output_path, report_path)
    if conflict is not None:
        print(f"error: {conflict}", file=sys.stderr)
        return 2

    errors: list[dict[str, Any]] = []
    segments: list[Any] | None = None
    audio: AudioInfo | None = None
    total_segments = 0

    # --- load and validate the plan -------------------------------------
    try:
        loaded = load_plan(plan_path)
    except PlanError as exc:
        errors.append(_error(-1, str(exc)))
    else:
        segments = loaded
        total_segments = len(segments)
        structurally_valid: dict[int, tuple[int, int]] = {}
        for index, segment in enumerate(segments):
            start_ms, end_ms, shape_error = check_segment_shape(index, segment)
            if shape_error is not None:
                errors.append(_error(index, shape_error))
            else:
                assert start_ms is not None and end_ms is not None
                structurally_valid[index] = (start_ms, end_ms)

    # --- load and validate the WAV --------------------------------------
    try:
        raw = input_path.read_bytes()
    except OSError as exc:
        errors.append(_error(-1, f"cannot read input WAV: {exc}"))
    else:
        audio, wav_issues = parse_wav(raw)
        for issue in wav_issues:
            errors.append(_error(-1, issue))

    # --- range-check every segment (all invalid ones, not just the first)
    if segments is not None and audio is not None:
        for index, (start_ms, end_ms) in structurally_valid.items():
            reason = segment_range_problem(
                start_ms, end_ms, audio.sample_rate, audio.sample_frames
            )
            if reason is not None:
                errors.append(_error(index, reason))

    errors.sort(key=lambda item: item["segment_index"])

    report: dict[str, Any] = {
        "total_segments": total_segments,
        "valid": False,
        "errors": errors,
    }

    wav_payload: bytes | None = None
    total_frames = 0
    if not errors:
        assert segments is not None and audio is not None
        try:
            wav_payload, total_frames = build_wav(
                audio, [structurally_valid[index] for index in range(len(segments))]
            )
        except ValueError as exc:
            errors.append(_error(-1, str(exc)))
            errors.sort(key=lambda item: item["segment_index"])
            report["errors"] = errors

    # --- overwrite policy (before any output file is created) -----------
    if not errors and output_path.exists() and not args.force:
        errors.append(
            _error(
                -1,
                f"output file already exists: {output_path}; "
                "refusing to overwrite without --force",
            )
        )
        errors.sort(key=lambda item: item["segment_index"])
        report["errors"] = errors

    report["valid"] = not errors

    # The report is written even for an invalid plan/input so the producer
    # can inspect the reasons, but only after all in-memory work succeeded.
    try:
        atomic_write(report_path, report_bytes(report))
    except OSError as exc:
        print(f"error: cannot write report {report_path}: {exc}", file=sys.stderr)
        return 1

    if not report["valid"] or wav_payload is None:
        _print_errors(report["errors"])
        print(
            f"not writing production WAV ({len(report['errors'])} error(s)); "
            f"report: {report_path}",
            file=sys.stderr,
        )
        return 1

    # --- write the production WAV atomically -----------------------------
    try:
        atomic_write(output_path, wav_payload)
    except OSError as exc:
        reason = f"cannot write output WAV: {exc}"
        report["errors"].append(_error(-1, reason))
        report["errors"].sort(key=lambda item: item["segment_index"])
        report["valid"] = False
        try:
            atomic_write(report_path, report_bytes(report))
        except OSError as report_exc:
            print(
                f"error: cannot update report after output failure: {report_exc}",
                file=sys.stderr,
            )
        print(f"error: {reason}", file=sys.stderr)
        return 1

    print(
        f"wrote {output_path} ({total_frames} frames, "
        f"{total_frames * 1000.0 / audio.sample_rate:.3f} ms); report: {report_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
