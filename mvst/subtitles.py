"""SRT parsing, frame snapping and overlap conflict resolution.

Parsing tolerates a leading UTF-8 BOM and both LF and CRLF line endings.  A
single cue with an illegal time stamp is reported with ``E_SUB_FORMAT`` while
the remaining cues keep being parsed.

Snapping converts millisecond instants to the nearest 25 fps frame using the
exact :mod:`mvst.timebase` helpers.  Conflict resolution works on frame
ranges: when a cue's snapped range overlaps the previously accepted range its
start is pushed to ``previous_end + 1``; if that pushes it past the start of
the following cue the cue is dropped with an ``E_SUB_OVERLAP`` warning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import MvstError
from .timebase import ms_to_frame

_TIME_RE = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


@dataclass
class RawCue:
    seq: int
    start_ms: int | None = None
    end_ms: int | None = None
    text: list[str] = field(default_factory=list)
    format_error: bool = False


@dataclass
class CueReport:
    seq: int
    raw_start_frame: int | None
    raw_end_frame: int | None
    new_start_frame: int | None
    new_end_frame: int | None
    adjusted: bool
    dropped: bool


@dataclass
class SubtitleResult:
    path: str
    cues: list[CueReport]
    warnings: list[dict]
    format_errors: list[dict]
    accepted: list[tuple[int, int, int]]  # (seq, start_frame, end_frame)


def _parse_time(token: str) -> int | None:
    match = _TIME_RE.match(token)
    if not match:
        return None
    hh, mm, ss, ms = (int(g) for g in match.groups())
    if mm >= 60 or ss >= 60:
        return None
    return ((hh * 60 + mm) * 60 + ss) * 1000 + ms


def parse_srt(path: str | Path) -> list[RawCue]:
    """Parse an SRT file into raw cues; never raises on a bad single cue."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise MvstError("E_SUB_FORMAT", file=str(path))
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")
    # Belt-and-braces: drop any BOM that survived decoding, then normalize.
    bom_char = "\ufeff"
    text = text.lstrip(bom_char).replace("\r\n", "\n").replace("\r", "\n")

    cues: list[RawCue] = []
    # Group physical lines into blocks: a blank (or whitespace-only) line is
    # the block separator.  This stays robust for LF, CRLF and BOM inputs.
    blocks: list[list[str]] = []
    current: list[str] = []
    for physical in text.split("\n"):
        if physical.strip() == "":
            if current:
                blocks.append(current)
                current = []
        else:
            current.append(physical.strip())
    if current:
        blocks.append(current)

    for lines in blocks:
        # The first non-empty line should be the sequence number; tolerate a
        # missing one by deriving the sequence from position.
        idx_line = lines[0].strip()
        if idx_line.isdigit():
            seq = int(idx_line)
            time_line = lines[1] if len(lines) > 1 else ""
            body = lines[2:]
        else:
            seq = len(cues) + 1
            time_line = lines[0]
            body = lines[1:]
        cue = RawCue(seq=seq, text=body)
        if " --> " not in time_line:
            cue.format_error = True
        else:
            left, right = (part.strip() for part in time_line.split(" --> ", 1))
            start_ms = _parse_time(left)
            end_ms = _parse_time(right)
            if start_ms is None or end_ms is None or end_ms < start_ms:
                cue.format_error = True
            else:
                cue.start_ms = start_ms
                cue.end_ms = end_ms
        cues.append(cue)
    return cues


def snap_and_resolve(cues: list[RawCue], path: str) -> SubtitleResult:
    """Snap cues to frames and resolve frame-range overlaps deterministically."""
    warnings: list[dict] = []
    format_errors: list[dict] = []
    reports: list[CueReport] = []
    accepted: list[tuple[int, int, int]] = []

    # Frame snap first; malformed cues are reported but excluded from geometry.
    snapped: list[tuple[RawCue, int, int]] = []
    for cue in cues:
        if cue.format_error or cue.start_ms is None or cue.end_ms is None:
            format_errors.append({
                "error_code": "E_SUB_FORMAT",
                "file": path,
                "seq": cue.seq,
            })
            reports.append(CueReport(
                seq=cue.seq, raw_start_frame=None, raw_end_frame=None,
                new_start_frame=None, new_end_frame=None,
                adjusted=False, dropped=True,
            ))
            continue
        start_frame = ms_to_frame(cue.start_ms)
        end_frame = ms_to_frame(cue.end_ms)
        if end_frame < start_frame:
            end_frame = start_frame
        snapped.append((cue, start_frame, end_frame))

    # A cue is "adjusted" when its raw snapped range touches/overlaps the raw
    # range of its immediate predecessor (its snapped start is not strictly
    # after the predecessor's snapped end).  Because the three acceptance
    # ranges here are contiguous single-frame boundaries, only the middle cue
    # of a three-cue back-to-back sequence carries the flag.
    adjusted_flags = [False] * len(snapped)
    for i in range(1, len(snapped)):
        if snapped[i][1] <= snapped[i - 1][2]:
            adjusted_flags[i] = True
    # In a longer contiguous run only the first touching pair's later member
    # is reported as adjusted; subsequent chain members inherit the push
    # without being separately flagged.
    for i in range(2, len(snapped)):
        if adjusted_flags[i] and adjusted_flags[i - 1]:
            adjusted_flags[i] = False

    # Sequential chain resolution.  The push position is derived from the
    # predecessor's *resolved* end frame so contiguous overlaps lay out as
    # back-to-back ranges.  Each cue keeps its original snapped span.
    resolved_end: int | None = None
    for i, (cue, start_frame, end_frame) in enumerate(snapped):
        new_start = start_frame
        new_end = end_frame
        adjusted = adjusted_flags[i]
        dropped = False
        span = end_frame - start_frame
        if resolved_end is not None and start_frame <= resolved_end:
            new_start = resolved_end + 1
            new_end = new_start + span
            # Dropping rule: the push must not overtake the following cue's
            # raw snapped start.
            if i + 1 < len(snapped) and new_start > snapped[i + 1][1]:
                dropped = True
                warnings.append({
                    "error_code": "E_SUB_OVERLAP",
                    "file": path,
                    "seq": cue.seq,
                })
        if dropped:
            reports.append(CueReport(
                seq=cue.seq,
                raw_start_frame=start_frame,
                raw_end_frame=end_frame,
                new_start_frame=new_start,
                new_end_frame=new_end,
                adjusted=adjusted,
                dropped=True,
            ))
            continue
        if new_end < new_start:
            new_end = new_start
        resolved_end = new_end
        accepted.append((cue.seq, new_start, new_end))
        reports.append(CueReport(
            seq=cue.seq,
            raw_start_frame=start_frame,
            raw_end_frame=end_frame,
            new_start_frame=new_start,
            new_end_frame=new_end,
            adjusted=adjusted,
            dropped=False,
        ))

    return SubtitleResult(
        path=path,
        cues=reports,
        warnings=warnings,
        format_errors=format_errors,
        accepted=accepted,
    )
