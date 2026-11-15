"""合成与核对主流程：解析清单 -> 逐动画出条带 -> 汇总核对结果。"""

from __future__ import annotations

import json
from pathlib import Path

from .builder import FrameLoadError, build_strip, save_strip
from .models import ParsedAnimation
from .parser import load_manifest

REPORT_FILENAME = "result.json"


def run_pipeline(manifest_path: str | Path, output_dir: str | Path) -> dict:
    """执行完整流程，返回可 JSON 序列化的核对结果字典。

    输出目录中的条带 PNG 完全由本次运行管理：本次未生成的旧条带会被清除，
    保证重复执行不残留过期文件。
    """
    manifest_path = Path(manifest_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    parsed_animations = load_manifest(manifest_path)
    expected_strips: set[str] = set()
    results: list[dict] = []

    for item in parsed_animations:
        results.append(_process_animation(item, output_dir, expected_strips))

    _remove_stale_strips(output_dir, expected_strips)

    report = {
        "manifest": str(manifest_path),
        "animation_count": len(results),
        "strip_count": len(expected_strips),
        "animations": sorted(results, key=lambda row: row["animation_id"]),
    }
    report_path = output_dir / REPORT_FILENAME
    with report_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return report


def _process_animation(
    item: ParsedAnimation, output_dir: Path, expected_strips: set[str]
) -> dict:
    """处理单个动画，返回一行核对结果。"""
    spec = item.spec
    issues = list(item.issues)
    row: dict = {
        "animation_id": item.animation_id,
        "target_duration_ms": item.target_duration_ms,
        "frame_count": len(spec.frames) if spec is not None else 0,
        "actual_total_ms": None,
        "diff_ms": None,
        "passed": False,
        "strip": None,
        "issues": issues,
    }

    if spec is None:
        # 结构校验被拒（目标时长非正数、序号重复等），不进入合成。
        return row

    actual_total = sum(frame.duration_ms for frame in spec.frames)
    row["actual_total_ms"] = actual_total
    row["diff_ms"] = actual_total - spec.target_duration_ms
    if row["diff_ms"] != 0:
        issues.append(
            f"时长配平偏差 {row['diff_ms']} 毫秒"
            f"（实际 {actual_total} / 目标 {spec.target_duration_ms}）"
        )

    try:
        strip, _ordered = build_strip(spec)
    except FrameLoadError as exc:
        issues.append(str(exc))
        return row

    strip_name = f"{spec.animation_id}.png"
    save_strip(strip, output_dir / strip_name)
    expected_strips.add(strip_name)
    row["strip"] = strip_name
    row["passed"] = not issues
    return row


def _remove_stale_strips(output_dir: Path, expected_strips: set[str]) -> None:
    """删除输出目录中不属于本次运行的过期条带 PNG。"""
    for path in output_dir.glob("*.png"):
        if path.name not in expected_strips:
            path.unlink()
