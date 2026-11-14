"""Per-line verification, report assembly and ordering."""

from __future__ import annotations

import json
import os
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import OnsetResult, WavError, detect_onset_ms, read_wav
from .inputs import DialogueEntry, DialogueList, FrameTable

STATUS_OK = "ok"
STATUS_OUT_OF_TOLERANCE = "out_of_tolerance"
STATUS_SILENT = "silent"
STATUS_UNVERIFIABLE = "unverifiable"
STATUS_PENDING = "pending"

CHECKED_STATUSES = (STATUS_OK, STATUS_OUT_OF_TOLERANCE)

STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_OUT_OF_TOLERANCE: "超差",
    STATUS_SILENT: "无声",
    STATUS_UNVERIFIABLE: "无法核对",
    STATUS_PENDING: "待处理问题",
}

EXPECTED_SAMPLE_RATE = 44100
EXPECTED_CHANNELS = 1


@dataclass(frozen=True)
class AudioProbe:
    """Decode + onset result for one WAV file, computed once per path."""

    onset: OnsetResult
    sample_rate: int
    channels: int
    error: str | None = None
    error_code: str | None = None


@dataclass
class LineResult:
    line_id: str
    status: str
    mouth_time_ms: float | None = None
    onset_ms: float | None = None
    offset_ms: float | None = None
    wav_path: str | None = None
    reason: str | None = None
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(value, 3)

        payload: dict[str, Any] = {
            "line_id": self.line_id,
            "mouth_time_ms": rounded(self.mouth_time_ms),
            "onset_ms": rounded(self.onset_ms),
            "offset_ms": rounded(self.offset_ms),
            "status": self.status,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.wav_path is not None:
            payload["wav_path"] = self.wav_path
        return payload


@dataclass
class VerificationResult:
    lines: list[LineResult]
    tolerance_ms: float
    frame_issues: list[dict[str, Any]] = field(default_factory=list)
    dialogue_issues: list[dict[str, Any]] = field(default_factory=list)
    unmatched_frame_lines: list[str] = field(default_factory=list)


class AudioProbeCache:
    """Decode each distinct WAV path at most once per run (requirement 6)."""

    def __init__(self) -> None:
        self._cache: dict[str, AudioProbe] = {}
        self.decode_count = 0

    def get(self, path: Path) -> AudioProbe:
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        self.decode_count += 1
        probe = self._probe(path)
        self._cache[key] = probe
        return probe

    @staticmethod
    def _probe(path: Path) -> AudioProbe:
        if not path.is_file():
            return AudioProbe(
                onset=OnsetResult(onset_ms=None, silent=False, threshold=0.0),
                sample_rate=0,
                channels=0,
                error=f"WAV 文件不存在: {path}",
                error_code="missing_wav_file",
            )
        try:
            decoded = read_wav(path)
        except WavError as exc:
            return AudioProbe(
                onset=OnsetResult(onset_ms=None, silent=False, threshold=0.0),
                sample_rate=0,
                channels=0,
                error=str(exc),
                error_code="decode_error",
            )
        onset = detect_onset_ms(decoded.samples, decoded.info.sample_rate)
        return AudioProbe(
            onset=onset,
            sample_rate=decoded.info.sample_rate,
            channels=decoded.info.channels,
        )


def _unverifiable(line_id: str, mouth_time_ms: float, wav_path: str | None, code: str, detail: str) -> LineResult:
    return LineResult(
        line_id=line_id,
        status=STATUS_UNVERIFIABLE,
        mouth_time_ms=mouth_time_ms,
        wav_path=wav_path,
        reason=code,
        detail=detail,
    )


def verify_line(
    entry: DialogueEntry,
    mouth_time_ms: float | None,
    tolerance_ms: float,
    cache: AudioProbeCache,
) -> LineResult:
    if mouth_time_ms is None:
        return LineResult(
            line_id=entry.line_id,
            status=STATUS_PENDING,
            wav_path=entry.wav_path,
            reason="no_mouth_time",
            detail="帧表中缺少该语句的口型时间点",
        )

    if entry.path_error is not None or entry.resolved_path is None:
        return LineResult(
            line_id=entry.line_id,
            status=STATUS_PENDING,
            mouth_time_ms=mouth_time_ms,
            wav_path=entry.wav_path,
            reason="missing_wav_path",
            detail=entry.path_error or "对白清单缺少有效 WAV 路径",
        )

    probe = cache.get(entry.resolved_path)
    wav_path_str = str(entry.resolved_path)

    if probe.error_code is not None:
        return _unverifiable(entry.line_id, mouth_time_ms, wav_path_str, probe.error_code, probe.error or "")

    # Requirement 7: only 44.1 kHz mono PCM is comparable.
    if probe.channels != EXPECTED_CHANNELS:
        return _unverifiable(
            entry.line_id,
            mouth_time_ms,
            wav_path_str,
            "channels_not_mono",
            f"声道数为 {probe.channels}，要求单声道（1）",
        )
    if probe.sample_rate != EXPECTED_SAMPLE_RATE:
        return _unverifiable(
            entry.line_id,
            mouth_time_ms,
            wav_path_str,
            "sample_rate_mismatch",
            f"采样率为 {probe.sample_rate} Hz，要求 {EXPECTED_SAMPLE_RATE} Hz",
        )

    # Requirement 5: a line whose whole signal stays below the silence floor is
    # listed separately and excluded from offset statistics.
    if probe.onset.silent:
        return LineResult(
            line_id=entry.line_id,
            status=STATUS_SILENT,
            mouth_time_ms=mouth_time_ms,
            wav_path=wav_path_str,
            reason="silent_audio",
            detail="整句电平低于静音底，未检测到起音点",
        )

    # Non-silent file but no run of energy long enough to call an onset.
    if probe.onset.onset_ms is None:
        return _unverifiable(
            entry.line_id,
            mouth_time_ms,
            wav_path_str,
            "no_onset_detected",
            "未找到持续超过静音底的起音段",
        )

    offset = mouth_time_ms - probe.onset.onset_ms
    status = STATUS_OUT_OF_TOLERANCE if abs(offset) > tolerance_ms else STATUS_OK
    return LineResult(
        line_id=entry.line_id,
        status=status,
        mouth_time_ms=mouth_time_ms,
        onset_ms=probe.onset.onset_ms,
        offset_ms=offset,
        wav_path=wav_path_str,
    )


def verify_all(
    frames: FrameTable,
    dialogues: DialogueList,
    tolerance_ms: float,
    cache: AudioProbeCache | None = None,
) -> VerificationResult:
    cache = cache or AudioProbeCache()
    results: list[LineResult] = []
    for line_id, entry in dialogues.entries.items():
        mouth_time = frames.earliest(line_id)
        results.append(verify_line(entry, mouth_time, tolerance_ms, cache))

    unmatched = sorted(set(frames.times_by_line) - set(dialogues.entries))
    return VerificationResult(
        lines=results,
        tolerance_ms=tolerance_ms,
        frame_issues=list(frames.issues),
        dialogue_issues=list(dialogues.issues),
        unmatched_frame_lines=unmatched,
    )


def sort_lines(lines: list[LineResult]) -> list[LineResult]:
    """Checked lines first, by |offset| descending, ties by line_id ascending.

    Non-checked lines (silent / unverifiable / pending) follow in line_id order
    so the report stays byte-for-byte deterministic across runs.
    """

    def key(line: LineResult) -> tuple[int, float, str]:
        if line.status in CHECKED_STATUSES and line.offset_ms is not None:
            return (0, -abs(line.offset_ms), line.line_id)
        return (1, 0.0, line.line_id)

    return sorted(lines, key=key)


def build_summary(result: VerificationResult) -> dict[str, Any]:
    lines = result.lines
    checked = [line for line in lines if line.status in CHECKED_STATUSES and line.offset_ms is not None]
    offsets = [line.offset_ms for line in checked if line.offset_ms is not None]

    silent = [line.line_id for line in lines if line.status == STATUS_SILENT]
    unverifiable = [line.line_id for line in lines if line.status == STATUS_UNVERIFIABLE]
    pending = [line.line_id for line in lines if line.status == STATUS_PENDING]
    out_ids = sorted(line.line_id for line in lines if line.status == STATUS_OUT_OF_TOLERANCE)

    if offsets:
        mean_offset = sum(offsets) / len(offsets)
        max_offset = max(offsets, key=abs)
        max_abs = max(abs(value) for value in offsets)
    else:
        mean_offset = None
        max_offset = None
        max_abs = None

    summary: dict[str, Any] = {
        "total_lines": len(lines),
        "checked_lines": len(checked),
        "out_of_tolerance_lines": len(out_ids),
        "out_of_tolerance_line_ids": out_ids,
        "mean_offset_ms": None if mean_offset is None else round(mean_offset, 3),
        "max_offset_ms": None if max_offset is None else round(max_offset, 3),
        "max_abs_offset_ms": None if max_abs is None else round(max_abs, 3),
        "silent_lines": len(silent),
        "silent_line_ids": silent,
        "unverifiable_lines": len(unverifiable),
        "unverifiable_line_ids": unverifiable,
        "pending_lines": len(pending),
        "pending_line_ids": pending,
        "tolerance_ms": result.tolerance_ms,
        "no_valid_lines": len(checked) == 0,
    }
    if len(checked) == 0:
        summary["message"] = "无有效语句：没有可参与偏移统计的语句"
    return summary


def build_report(result: VerificationResult) -> dict[str, Any]:
    ordered = sort_lines(result.lines)
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tolerance_ms": result.tolerance_ms,
        "summary": build_summary(result),
        "lines": [line.to_json() for line in ordered],
    }
    if result.unmatched_frame_lines:
        report["unmatched_frame_lines"] = result.unmatched_frame_lines
    issues = result.frame_issues + result.dialogue_issues
    if issues:
        report["input_issues"] = issues
    return report


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, target)
    return target


def _display_width(text: str) -> int:
    """Terminal column width, treating CJK characters and full-width marks as 2."""
    width = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_console_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = report["lines"]

    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value}"

    rows = [
        f"{_pad('语句标识', 17)} {_pad('状态', 12)} {'口型(ms)':>9} {'起音(ms)':>8} {'偏移(ms)':>8}  说明",
        "-" * 78,
    ]
    status_labels = STATUS_LABELS
    for line in lines:
        label = status_labels.get(line["status"], line["status"])
        rows.append(
            f"{_pad(line['line_id'], 17)} {_pad(label, 12)}"
            f" {fmt(line.get('mouth_time_ms')):>9} {fmt(line.get('onset_ms')):>8} {fmt(line.get('offset_ms')):>8}"
            f"  {line.get('reason', '')}"
        )

    rows.append("")
    rows.append(
        f"参与统计 {summary['checked_lines']} 句，超差 {summary['out_of_tolerance_lines']} 句，"
        f"平均偏移 {fmt(summary['mean_offset_ms'])} ms，最大偏移 {fmt(summary['max_offset_ms'])} ms"
    )
    if summary["silent_lines"]:
        rows.append(f"无声语句: {', '.join(summary['silent_line_ids'])}")
    if summary["unverifiable_lines"]:
        rows.append(f"无法核对: {', '.join(summary['unverifiable_line_ids'])}")
    if summary["pending_lines"]:
        rows.append(f"待处理问题: {', '.join(summary['pending_line_ids'])}")
    if summary.get("message"):
        rows.append(summary["message"])
    return "\n".join(rows)
