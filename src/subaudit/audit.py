"""逐句字幕 vs 人声发声区间核查。

输出结构（JSON 可序列化）：

{
  "status": "complete" | "incomplete",
  "parameters": {...},
  "results": [ 逐句结论，按清单顺序 ],
  "invalid_entries": [ 句号重复/起止倒置/非数字等无法核查的条目 ],
  "flagged": [ 未覆盖语音或偏差超容忍的句号 ],
  "failures": [ 单句处理异常（注入或真实错误）],
  "errors": [ 运行级错误说明 ]
}
"""

from __future__ import annotations

import math
import numbers
import os
from typing import Any

from .vad import VoiceSegment, detect_voice_segments, find_voice_in_window

# 注入单句异常的测试钩子：把句号放进该环境变量，处理该句时抛 RuntimeError。
FAIL_HOOK_ENV = "SUBAUDIT_FORCE_FAIL_SENTENCE"

DEFAULT_TOLERANCE_MS = 200.0
DEFAULT_ASSIGN_MARGIN_MS = 500.0


def _is_real_number(value: Any) -> bool:
    """清单字段是否为可用数字（拒绝 bool、NaN、Inf、str 等）。"""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        return False
    return math.isfinite(float(value))


def validate_entry(entry: Any, seen_ids: set) -> str | None:
    """校验单条字幕清单，返回错误原因；合法返回 None。"""
    if not isinstance(entry, dict):
        return "条目不是 JSON 对象"

    missing = [k for k in ("id", "start_ms", "end_ms") if k not in entry]
    if missing:
        return f"缺少字段: {', '.join(missing)}"

    sentence_id = entry.get("id")
    if sentence_id is None or (isinstance(sentence_id, str) and not sentence_id.strip()):
        return "句号为空"
    if sentence_id in seen_ids:
        return f"句号重复: {sentence_id}"

    start = entry.get("start_ms")
    end = entry.get("end_ms")
    if not _is_real_number(start):
        return "字幕起点不是有效数字"
    if not _is_real_number(end):
        return "字幕终点不是有效数字"
    if float(start) < 0 or float(end) < 0:
        return "字幕时间不能为负"
    if float(start) >= float(end):
        return f"字幕起止倒置: start_ms={start} >= end_ms={end}"
    return None


def _judge(
    sub_start: float,
    sub_end: float,
    voice: VoiceSegment,
    tolerance_ms: float,
) -> tuple[str, list[str]]:
    """根据偏差与容忍范围给出核查结论与问题标签。"""
    issues: list[str] = []
    if voice.start_ms < sub_start:
        issues.append("voice_leading_subtitle")   # 人声先于字幕开始（字幕漏盖头部）
    if voice.end_ms > sub_end:
        issues.append("voice_trailing_subtitle")  # 人声晚于字幕结束（字幕漏盖尾部）
    if sub_start < voice.start_ms - tolerance_ms:
        issues.append("subtitle_starts_too_early")
    if sub_end > voice.end_ms + tolerance_ms:
        issues.append("subtitle_ends_too_late")
    conclusion = "ok" if not issues else "out_of_tolerance"
    return conclusion, issues


def audit_manifest(
    samples,
    sample_rate: int,
    manifest: list,
    *,
    tolerance_ms: float = DEFAULT_TOLERANCE_MS,
    assign_margin_ms: float = DEFAULT_ASSIGN_MARGIN_MS,
    vad_params: dict | None = None,
) -> dict:
    """对整份字幕清单执行逐句核查。

    单条异常不会中断整体流程：异常条目进入 failures，其余句子结论照常给出。
    """
    vad_kwargs = dict(vad_params or {})
    parameters = {
        "sample_rate": sample_rate,
        "tolerance_ms": tolerance_ms,
        "assign_margin_ms": assign_margin_ms,
        "vad": {
            "frame_ms": vad_kwargs.get("frame_ms", 20.0),
            "hop_ms": vad_kwargs.get("hop_ms", 10.0),
            "abs_rms": vad_kwargs.get("abs_rms", 0.01),
            "rel_factor": vad_kwargs.get("rel_factor", 4.0),
            "min_voiced_ms": vad_kwargs.get("min_voiced_ms", 60.0),
            "merge_gap_ms": vad_kwargs.get("merge_gap_ms", 120.0),
        },
    }

    report: dict = {
        "status": "complete",
        "parameters": parameters,
        "results": [],
        "invalid_entries": [],
        "flagged": [],
        "failures": [],
        "errors": [],
    }

    # VAD 只跑一次，保证确定性与效率。
    try:
        segments = detect_voice_segments(samples, sample_rate, **vad_kwargs)
    except Exception as exc:  # VAD 整体失败：仍输出结构化的不完整报告
        report["status"] = "incomplete"
        report["errors"].append(f"语音检测失败: {type(exc).__name__}: {exc}")
        return report

    force_fail = os.environ.get(FAIL_HOOK_ENV, "").strip()
    seen_ids: set = set()
    used_segments: set = set()

    for index, entry in enumerate(manifest):
        order = index + 1
        try:
            reason = validate_entry(entry, seen_ids)
            if reason is not None:
                report["invalid_entries"].append(
                    {"order": order, "entry": entry, "reason": reason}
                )
                # 合法句号才登记，避免倒置条目污染重复判定之外的逻辑。
                sid = entry.get("id") if isinstance(entry, dict) else None
                if sid is not None and sid not in seen_ids and "句号重复" not in reason:
                    seen_ids.add(sid)
                continue

            sentence_id = entry["id"]
            seen_ids.add(sentence_id)

            if force_fail and str(sentence_id) == force_fail:
                raise RuntimeError(f"注入的单句处理异常（句号 {sentence_id}）")

            sub_start = float(entry["start_ms"])
            sub_end = float(entry["end_ms"])

            candidate = find_voice_in_window(segments, sub_start, sub_end)
            # 防止同一段人声被相邻两句重复认领。
            if candidate is not None and candidate.as_tuple() in used_segments:
                nearby = [
                    seg for seg in segments if seg.as_tuple() not in used_segments
                ]
                candidate = find_voice_in_window(nearby, sub_start, sub_end)
            if candidate is not None:
                gap = max(
                    sub_start - candidate.end_ms,
                    candidate.start_ms - sub_end,
                    0.0,
                )
                if gap > assign_margin_ms:
                    candidate = None

            result: dict = {
                "order": order,
                "id": sentence_id,
                "text": entry.get("text", ""),
                "subtitle_start_ms": sub_start,
                "subtitle_end_ms": sub_end,
                "voice_start_ms": None,
                "voice_end_ms": None,
                "start_deviation_ms": None,
                "end_deviation_ms": None,
                "conclusion": "",
                "issues": [],
            }

            if candidate is None:
                result["conclusion"] = "no_voice_detected"
                result["issues"] = ["no_voice_detected"]
            else:
                used_segments.add(candidate.as_tuple())
                result["voice_start_ms"] = candidate.start_ms
                result["voice_end_ms"] = candidate.end_ms
                # 偏差 = 字幕时间 - 语音时间；负值表示字幕领先人声，正值表示拖尾。
                result["start_deviation_ms"] = _round1(
                    sub_start - candidate.start_ms
                )
                result["end_deviation_ms"] = _round1(sub_end - candidate.end_ms)
                result["conclusion"], result["issues"] = _judge(
                    sub_start, sub_end, candidate, tolerance_ms
                )

            report["results"].append(result)
            if result["conclusion"] != "ok":
                report["flagged"].append(sentence_id)

        except Exception as exc:  # 单句隔离：已完成结果保持可用
            report["status"] = "incomplete"
            report["failures"].append(
                {
                    "order": order,
                    "id": entry.get("id") if isinstance(entry, dict) else None,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )

    return report


def _round1(value: float) -> float:
    """偏差保留 1 位小数，规避浮点序列化噪声。"""
    return float(round(float(value) + 0.0, 1))
