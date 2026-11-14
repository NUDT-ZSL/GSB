# -*- coding: utf-8 -*-
"""字幕清单换算与打包工具。

读取字幕清单 JSON 与换算参数 JSON，按目标帧率把每条字幕的起止时间
换算为帧号，整理成可导入的字幕包，并输出核对结果（重叠、越界、
字段非法、零长/倒置等）。

用法：
    python src/subtitle_packager.py --manifest samples/manifest.json \
        --params samples/params.json --out-dir out

清单格式（manifest JSON）：
    {
      "entries": [
        {"track": 1, "text": "……", "start": 1.0, "end": 3.5, "unit": "seconds"},
        ...
      ]
    }
    unit 支持 "seconds" / "milliseconds" / "frames"（源帧，需配合 source_fps）。

换算参数（params JSON）：
    {
      "target_fps": 25,        # 目标帧率（必填）
      "source_fps": 30,        # 源帧率，unit 为 frames 时使用（必填）
      "head_seconds": 0.0,     # 片头边界，秒（可选，默认 0）
      "tail_seconds": 600.0    # 片尾边界，秒（必填）
    }

换算规则：帧号 = round_half_up(秒 * target_fps)，即四舍五入到最近帧；
负帧号一律钳制为 0 并打标，不会在包内出现负帧号。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SUPPORTED_UNITS = ("seconds", "milliseconds", "frames")

# 条目状态标记（写入包内 entries[].flags 与核对报告）
FLAG_OVERLAP = "overlap"            # 同轨时间重叠
FLAG_BEFORE_HEAD = "before_head"    # 起始早于片头
FLAG_PAST_TAIL = "past_tail"        # 终止晚于片尾
FLAG_ZERO_LENGTH = "zero_length"    # 起止相等
FLAG_INVERTED = "inverted"          # 起止倒置
FLAG_CLAMPED = "clamped_to_zero"    # 换算出负帧号，已钳制为 0


def round_half_up(value: float) -> int:
    """四舍五入到最近整数（0.5 向上），保证换算结果确定性。"""
    return int(math.floor(value + 0.5))


def seconds_to_frame(seconds: float, fps: float) -> int:
    return round_half_up(seconds * fps)


def to_seconds(value: float, unit: str, source_fps: float) -> float:
    if unit == "seconds":
        return float(value)
    if unit == "milliseconds":
        return float(value) / 1000.0
    if unit == "frames":
        return float(value) / source_fps
    raise ValueError(f"unsupported unit: {unit}")


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_track(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate_entry(entry) -> list[str]:
    """校验单条清单字段，返回问题描述列表（空列表表示合法）。"""
    problems: list[str] = []
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    if not _is_track(entry.get("track")):
        problems.append("field 'track' missing or not a non-negative integer")
    if not isinstance(entry.get("text"), str):
        problems.append("field 'text' missing or not a string")
    if not _is_number(entry.get("start")):
        problems.append("field 'start' missing or not a finite number")
    if not _is_number(entry.get("end")):
        problems.append("field 'end' missing or not a finite number")
    if entry.get("unit") not in SUPPORTED_UNITS:
        problems.append(
            f"field 'unit' missing or not one of {list(SUPPORTED_UNITS)}"
        )
    return problems


def validate_params(params) -> list[str]:
    problems: list[str] = []
    if not isinstance(params, dict):
        return ["params is not an object"]
    if not _is_number(params.get("target_fps")) or params["target_fps"] <= 0:
        problems.append("field 'target_fps' missing or not a positive number")
    if not _is_number(params.get("source_fps")) or params["source_fps"] <= 0:
        problems.append("field 'source_fps' missing or not a positive number")
    if not _is_number(params.get("tail_seconds")):
        problems.append("field 'tail_seconds' missing or not a finite number")
    head = params.get("head_seconds", 0.0)
    if not _is_number(head):
        problems.append("field 'head_seconds' is not a finite number")
    return problems


def build_package(manifest: dict, params: dict) -> tuple[dict, dict]:
    """核心流程：校验 -> 换算 -> 标记 -> 排序 -> 汇总。

    返回 (subtitle_package, check_report)。
    """
    target_fps = float(params["target_fps"])
    source_fps = float(params["source_fps"])
    head_frame = seconds_to_frame(float(params.get("head_seconds", 0.0)), target_fps)
    tail_frame = seconds_to_frame(float(params["tail_seconds"]), target_fps)

    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("manifest field 'entries' missing or not a list")
    if len(raw_entries) > 300:
        raise ValueError("manifest has more than 300 entries")

    problems: list[dict] = []   # 字段非法条目（不进包）
    converted: list[dict] = []  # 合法条目（进包，可能带 flags）

    for index, entry in enumerate(raw_entries):
        field_problems = validate_entry(entry)
        if field_problems:
            problems.append({"index": index, "reasons": field_problems})
            continue

        start_frame = seconds_to_frame(
            to_seconds(entry["start"], entry["unit"], source_fps), target_fps
        )
        end_frame = seconds_to_frame(
            to_seconds(entry["end"], entry["unit"], source_fps), target_fps
        )

        flags: list[str] = []
        # 零长 / 倒置（先判定，再钳制负帧号）
        if end_frame < start_frame:
            flags.append(FLAG_INVERTED)
        elif end_frame == start_frame:
            flags.append(FLAG_ZERO_LENGTH)
        # 负帧号钳制：包内不允许出现负帧号
        if start_frame < 0:
            start_frame = 0
            flags.append(FLAG_CLAMPED)
        if end_frame < 0:
            end_frame = 0
            flags.append(FLAG_CLAMPED)
        # 片头片尾越界（保留原条目与原文案，仅标注方向）
        if start_frame < head_frame:
            flags.append(FLAG_BEFORE_HEAD)
        if end_frame > tail_frame:
            flags.append(FLAG_PAST_TAIL)

        converted.append(
            {
                "index": index,  # 原始清单下标，便于定位
                "track": entry["track"],
                "text": entry["text"],  # 原文完整保留，不截断
                "start_frame": start_frame,
                "end_frame": end_frame,
                "flags": flags,
            }
        )

    # 同轨重叠检测：半开区间 [start_frame, end_frame)，相邻不算重叠；
    # 不同轨道互不影响。重叠条目不合并、不删除，双方均打标。
    overlaps: list[dict] = []
    by_track: dict[int, list[dict]] = {}
    for item in converted:
        by_track.setdefault(item["track"], []).append(item)
    for track, items in sorted(by_track.items()):
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["start_frame"] < b["end_frame"] and b["start_frame"] < a["end_frame"]:
                    for item in (a, b):
                        if FLAG_OVERLAP not in item["flags"]:
                            item["flags"].append(FLAG_OVERLAP)
                    overlaps.append(
                        {"track": track, "indices": [a["index"], b["index"]]}
                    )

    # 排序：轨道号升序，同轨按起始帧升序，再按原始下标保证稳定
    converted.sort(key=lambda e: (e["track"], e["start_frame"], e["index"]))

    def _indices_with(flag: str) -> list[int]:
        return [e["index"] for e in converted if flag in e["flags"]]

    package = {
        "target_fps": params["target_fps"],
        "head_frame": head_frame,
        "tail_frame": tail_frame,
        "entries": [
            {
                "track": e["track"],
                "text": e["text"],
                "start_frame": e["start_frame"],
                "end_frame": e["end_frame"],
                "flags": e["flags"],
                "source_index": e["index"],
            }
            for e in converted
        ],
    }

    ok_count = sum(1 for e in converted if not e["flags"])
    report = {
        "summary": {
            "total": len(raw_entries),
            "ok": ok_count,
            "flagged": len(converted) - ok_count,
            "invalid": len(problems),
        },
        "invalid_entries": problems,
        "overlaps": overlaps,
        "out_of_range": {
            "before_head": _indices_with(FLAG_BEFORE_HEAD),
            "past_tail": _indices_with(FLAG_PAST_TAIL),
        },
        "anomalies": {
            "zero_length": _indices_with(FLAG_ZERO_LENGTH),
            "inverted": _indices_with(FLAG_INVERTED),
            "clamped_to_zero": _indices_with(FLAG_CLAMPED),
        },
    }
    return package, report


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="字幕清单换算与打包工具")
    parser.add_argument("--manifest", required=True, type=Path, help="字幕清单 JSON")
    parser.add_argument("--params", required=True, type=Path, help="换算参数 JSON")
    parser.add_argument("--out-dir", required=True, type=Path, help="输出目录")
    args = parser.parse_args(argv)

    manifest = load_json(args.manifest)
    params = load_json(args.params)

    param_problems = validate_params(params)
    if param_problems:
        for p in param_problems:
            print(f"参数错误: {p}", file=sys.stderr)
        return 2

    package, report = build_package(manifest, params)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    package_path = args.out_dir / "subtitle_package.json"
    report_path = args.out_dir / "check_report.json"
    package_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    s = report["summary"]
    print(f"字幕包: {package_path}")
    print(f"核对报告: {report_path}")
    print(
        f"总条目 {s['total']}，正常 {s['ok']}，"
        f"带标记 {s['flagged']}，字段非法 {s['invalid']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
