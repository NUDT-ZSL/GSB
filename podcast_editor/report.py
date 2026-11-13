"""编辑报告生成。

报告为结构化 JSON，区分「完整成功」与「不完整（部分失败但部分可用）」，
每个失败都带稳定错误码与人类可读消息；缺失数据显式标注，绝不伪装成空结果。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .engine import EditorEngine
from .wavio import SAMPLE_MAX


def build_report(
    engine: EditorEngine,
    *,
    instruction_file: str,
    output_wav: str,
    resumed: bool,
) -> dict:
    """根据引擎最终状态组装报告字典。"""
    results = engine.results
    failed = [r for r in results if r.status == "failed"]
    succeeded = [r for r in results if r.status == "success"]

    output_exists = (
        engine.sample_rate is not None
        and engine.frame_count > 0
        and os.path.exists(output_wav)
    )

    if not results:
        overall_status = "empty"          # 指令数组本身为空
    elif not failed:
        overall_status = "complete"       # 全部成功
    elif output_exists:
        overall_status = "incomplete"     # 有失败，但成功部分已产出可用 WAV
    else:
        overall_status = "failed"         # 全部失败，时间线无任何数据

    if output_exists:
        output_info = {
            "path": os.path.abspath(output_wav),
            "exists": True,
            "sample_rate": engine.sample_rate,
            "channels": 1,
            "sample_width_bits": 16,
            "frame_count": engine.frame_count,
            "duration_seconds": engine.timeline_duration,
            "max_allowed_amplitude": SAMPLE_MAX,
        }
    else:
        # 显式声明无输出，而不是省略字段或返回空 WAV
        output_info = {
            "path": os.path.abspath(output_wav),
            "exists": False,
            "reason": "没有任何成功指令建立时间线，未生成输出 WAV",
        }

    return {
        "report_generated_at": datetime.now(timezone.utc).isoformat(),
        "instruction_file": os.path.abspath(instruction_file),
        "overall_status": overall_status,
        "resumed_from_checkpoint": resumed,
        "summary": {
            "total_instructions": len(results),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "resumed_skipped": sum(1 for r in results if r.resumed),
            "total_duration_seconds": engine.timeline_duration,
            "total_frames": engine.frame_count,
            "sample_rate": engine.sample_rate,
        },
        "output": output_info,
        "unavailable_sources": engine.unavailable_sources(),
        "instructions": [
            {
                "index": r.index,
                "type": r.type,
                "source": r.source,
                "params": r.params,
                "status": r.status,
                "applied": r.applied,
                "resumed_skipped": r.resumed,
                "detail": r.detail,
                "timeline_duration_after_seconds": r.output_timeline_duration,
                "error": r.error,   # 成功时为 null；失败时 {code, message, retryable}
            }
            for r in results
        ],
    }


def write_report(report: dict, path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
