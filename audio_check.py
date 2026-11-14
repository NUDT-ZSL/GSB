#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""战斗音效素材核对工具

读取素材清单 JSON，逐条核对 WAV 的实际时长与峰值电平：
- 时长不在允许范围内 -> 结论「需复核」；
- 峰值偏差在允许范围内 -> 结论「可用」，不生成试听文件；
- 峰值偏差超出允许范围 -> 按目标峰值做线性增益（时长与波形形状不变），
  生成调整后的试听 WAV，结论「需复核」；
- 清单/文件/参数异常 -> 结论「需复核」并在原因中说明，不阻断其余素材。

输出报告 JSON：顶部汇总可用/需复核数量，逐条列出 id、实际时长、实际峰值、
目标峰值、调整后峰值、结论与原因，顺序与清单一致。

用法：
    python audio_check.py --manifest samples/manifest.json \
        --audio-out out/audio --report out/report.json [--peak-tolerance 0.01]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import wave
from pathlib import Path

import numpy as np

MAX_DURATION_MS = 30000          # 单条素材允许核对的最大时长（毫秒）
INT16_FULL_SCALE = 32768.0       # 16 位 PCM 满量程
REQUIRED_FIELDS = ("id", "path", "target_peak", "duration_min_ms", "duration_max_ms")

OK = "可用"
REVIEW = "需复核"


def fail(msg: str, code: int = 2) -> None:
    """清单级致命错误：打印可读说明并退出，不生成任何文件。"""
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def read_wav(path: Path):
    """读取 16 位 PCM WAV，返回 (采样率, 声道数, 帧数, int32 采样数组)。"""
    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        rate = wf.getframerate()
        width = wf.getsampwidth()
        frames = wf.getnframes()
        raw = wf.readframes(frames)
    if width != 2:
        raise ValueError(f"仅支持 16 位 PCM，实际采样位深为 {width * 8} 位")
    data = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    if channels > 1:
        data = data.reshape(-1, channels)
    return rate, channels, frames, data


def write_wav(path: Path, rate: int, channels: int, data_int: np.ndarray) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(data_int, dtype="<i2").tobytes())


def peak_of(data_int: np.ndarray) -> float:
    if data_int.size == 0:
        return 0.0
    return float(np.max(np.abs(data_int))) / INT16_FULL_SCALE


def adjust_peak(data_int: np.ndarray, actual_peak: float, target_peak: float) -> np.ndarray:
    """线性增益调整峰值：波形形状与时长不变；结果限制在满量程内，绝不削波。"""
    gain = target_peak / actual_peak
    scaled = np.round(data_int.astype(np.float64) * gain)
    # 目标峰值 <= 1，增益后绝对值不超过 round(32767*gain*32768/32768)<=32767，
    # 此处 clip 仅作兜底，保证任何采样绝对值不超过满量程。
    return np.clip(scaled, -32768, 32767).astype(np.int32)


def safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]", "_", text)


def load_manifest(manifest_path: Path) -> list:
    if not manifest_path.is_file():
        fail(f"清单文件不存在: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"清单 JSON 解析失败: 第 {e.lineno} 行第 {e.colno} 列: {e.msg}（文件: {manifest_path}）")
    except UnicodeDecodeError as e:
        fail(f"清单文件编码不是有效的 UTF-8: {e}（文件: {manifest_path}）")
    if not isinstance(manifest, list):
        fail("清单顶层结构必须是 JSON 数组 [...]")
    seen_ids = set()
    for i, item in enumerate(manifest):
        if not isinstance(item, dict):
            fail(f"清单第 {i + 1} 条不是 JSON 对象")
        for field in REQUIRED_FIELDS:
            if field not in item:
                fail(f"清单第 {i + 1} 条缺少必需字段 '{field}'"
                     f"（id={item.get('id', '<无id>')}）")
        if not isinstance(item["id"], str) or not item["id"]:
            fail(f"清单第 {i + 1} 条字段 'id' 必须是非空字符串")
        if item["id"] in seen_ids:
            fail(f"清单第 {i + 1} 条 id '{item['id']}' 与前面条目重复")
        seen_ids.add(item["id"])
        if not isinstance(item["path"], str) or not item["path"]:
            fail(f"清单第 {i + 1} 条（id={item['id']}）字段 'path' 必须是非空字符串")
        for field in ("target_peak", "duration_min_ms", "duration_max_ms"):
            value = item[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                fail(f"清单第 {i + 1} 条（id={item['id']}）字段 '{field}' 必须是数值")
    return manifest


def new_record(item: dict) -> dict:
    return {
        "id": item["id"],
        "path": item["path"],
        "actual_duration_ms": None,
        "actual_peak": None,
        "target_peak": item["target_peak"],
        "adjusted_peak": None,
        "conclusion": REVIEW,
        "reason": "",
        # 以下为内部字段，写入报告前删除
        "_reasons": [],
        "_format": None,
        "_wav": None,       # (rate, channels, data_int)
        "_needs_gain": False,
    }


def validate_values(item: dict, rec: dict) -> None:
    """取值合法性核对（不读文件）。不合法时记录原因，该条不可核对。"""
    target = item["target_peak"]
    dmin = item["duration_min_ms"]
    dmax = item["duration_max_ms"]
    if not (0 < target <= 1):
        rec["_reasons"].append(
            f"目标峰值电平越界（{target}），须在 (0, 1] 区间内，该条不可核对")
    if dmin < 0:
        rec["_reasons"].append(f"允许时长下限为负（{dmin}ms），该条不可核对")
    if dmin > dmax:
        rec["_reasons"].append(
            f"允许时长范围起止颠倒（下限 {dmin}ms 大于上限 {dmax}ms），该条不可核对")
    if dmax > MAX_DURATION_MS:
        rec["_reasons"].append(
            f"允许时长上限 {dmax}ms 超过 {MAX_DURATION_MS}ms 核对范围，该条不可核对")


def inspect_file(item: dict, rec: dict, manifest_dir: Path) -> None:
    """读取 WAV，核对时长，记录实际峰值。"""
    wav_path = Path(item["path"])
    if not wav_path.is_absolute():
        wav_path = manifest_dir / wav_path
    if not wav_path.is_file():
        rec["_reasons"].append(f"WAV 文件缺失: {wav_path}")
        return
    try:
        rate, channels, frames, data = read_wav(wav_path)
    except (wave.Error, EOFError, ValueError, OSError) as e:
        rec["_reasons"].append(f"WAV 文件头损坏或无法解析: {wav_path}（{e}）")
        return
    duration_ms = frames / rate * 1000.0
    rec["actual_duration_ms"] = round(duration_ms, 3)
    rec["actual_peak"] = round(peak_of(data), 6)
    rec["_format"] = (rate, channels)
    rec["_wav"] = (rate, channels, data)
    if duration_ms > MAX_DURATION_MS:
        rec["_reasons"].append(
            f"实际时长 {round(duration_ms, 1)}ms 超过 {MAX_DURATION_MS}ms 核对范围，该条不可核对")
        return
    dmin = item["duration_min_ms"]
    dmax = item["duration_max_ms"]
    if not (dmin <= duration_ms <= dmax):
        rec["_reasons"].append(
            f"实际时长 {round(duration_ms, 1)}ms 不在允许范围 [{dmin}, {dmax}]ms 内")


def check_peak(item: dict, rec: dict, tolerance: float, audio_out: Path,
               formats_consistent: bool) -> None:
    """峰值核对与试听文件生成（仅对前置核对全部通过的条目调用）。"""
    actual = rec["actual_peak"]
    target = item["target_peak"]
    if abs(actual - target) <= tolerance:
        rec["conclusion"] = OK
        rec["adjusted_peak"] = actual
        rec["_reasons"].append(
            f"峰值偏差 {round(abs(actual - target), 6)} 在允许范围 ±{tolerance} 内，无需调整")
        return
    rec["conclusion"] = REVIEW
    if actual == 0:
        rec["_reasons"].append("素材为静音（峰值为 0），无法按比例调整至目标峰值")
        return
    rec["_reasons"].append(
        f"峰值偏差超出允许范围（实际 {actual}，目标 {target}，容差 ±{tolerance}）")
    if not formats_consistent:
        rec["_reasons"].append("因清单内素材采样率/声道数不一致，本次未生成试听文件")
        return
    rate, channels, data = rec["_wav"]
    adjusted = adjust_peak(data, actual, target)
    out_path = audio_out / f"{safe_name(rec['id'])}_adjusted.wav"
    write_wav(out_path, rate, channels, adjusted)
    rec["adjusted_peak"] = round(peak_of(adjusted), 6)
    rec["_reasons"].append(f"已生成试听文件: {out_path.name}")


def run(manifest_path: Path, audio_out: Path, report_path: Path, tolerance: float) -> int:
    manifest = load_manifest(manifest_path)
    manifest_dir = manifest_path.resolve().parent
    # 清单结构合法后再创建输出目录，避免失败运行留下空目录
    audio_out.mkdir(parents=True, exist_ok=True)

    if len(manifest) == 0:
        print("[说明] 清单条数为 0，无素材需要核对，不生成任何试听文件。")

    records = []
    for item in manifest:
        rec = new_record(item)
        validate_values(item, rec)
        if not rec["_reasons"]:
            inspect_file(item, rec, manifest_dir)
        records.append(rec)

    # 采样率/声道数一致性核对：不一致时说明具体差异，且不生成任何试听文件
    readable = [r for r in records if r["_format"] is not None and not r["_reasons"]]
    formats = [r["_format"] for r in readable]
    formats_consistent = len(set(formats)) <= 1
    if not formats_consistent:
        reference = max(set(formats), key=formats.count)
        for r in readable:
            rate, ch = r["_format"]
            if r["_format"] != reference:
                r["_reasons"].append(
                    f"采样率/声道数与清单基准不一致（本条 {rate}Hz/{ch}声道，"
                    f"基准 {reference[0]}Hz/{reference[1]}声道）")
        print(f"[说明] 清单内素材格式不一致（基准 {reference[0]}Hz/{reference[1]}声道），"
              f"本次不生成试听文件。")

    for item, rec in zip(manifest, records):
        if rec["_reasons"]:
            continue  # 前置核对未通过，不做峰值核对
        check_peak(item, rec, tolerance, audio_out, formats_consistent)

    # 汇总与报告
    for rec in records:
        rec["reason"] = "；".join(rec["_reasons"])
        for key in ("_reasons", "_format", "_wav", "_needs_gain"):
            rec.pop(key, None)
    usable = sum(1 for r in records if r["conclusion"] == OK)
    review = len(records) - usable
    report = {
        "summary": {"total": len(records), "usable": usable, "needs_review": review},
        "items": records,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # 使用带 BOM 的 UTF-8，便于 Windows 下各类编辑器正确识别中文
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8-sig")

    for rec in records:
        print(f"  [{rec['conclusion']}] {rec['id']}: {rec['reason']}")
    print(f"核对完成：共 {len(records)} 条，可用 {usable} 条，需复核 {review} 条。")
    print(f"报告: {report_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="战斗音效素材时长/峰值核对工具")
    parser.add_argument("--manifest", required=True, help="素材清单 JSON 路径")
    parser.add_argument("--audio-out", required=True, help="试听 WAV 输出目录（不存在时自动创建）")
    parser.add_argument("--report", required=True, help="核对报告 JSON 输出路径")
    parser.add_argument("--peak-tolerance", type=float, default=0.01,
                        help="峰值允许偏差（线性，默认 0.01）")
    args = parser.parse_args()
    if not (0 < args.peak_tolerance < 1):
        fail(f"峰值容差须在 (0, 1) 内，当前为 {args.peak_tolerance}")

    sys.exit(run(Path(args.manifest), Path(args.audio_out),
                 Path(args.report), args.peak_tolerance))


if __name__ == "__main__":
    main()
