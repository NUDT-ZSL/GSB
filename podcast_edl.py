#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""podcast_edl.py — 播客采访录音批量剪辑工具

读取 JSON 剪辑清单(EDL), 从 16 位 PCM 单声道 WAV 源文件中裁剪片段,
可选峰值归一化, 按清单顺序拼接为新的 WAV, 并生成逐条 JSON 处理报告。

用法:
    python podcast_edl.py <剪辑清单.json> <输出目录>

退出码:
    0 — 全部条目成功
    1 — 任一条目被跳过或出错(含采样率不一致整体拒绝)
    2 — 清单本身无法解析等致命错误
"""
from __future__ import annotations

import argparse
import array
import json
import sys
import wave
from pathlib import Path

MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 48000
NORMALIZE_TARGET = 29491  # 16 位满量程(32767)的 90% 取整目标峰值
REQUIRED_FIELDS = ("source", "start", "end", "normalize")
OUTPUT_WAV_NAME = "output.wav"
REPORT_NAME = "report.json"


class FatalError(Exception):
    """清单级致命错误, 无法开始任何处理。"""


class SourceError(Exception):
    """源文件级错误: 不存在 / 损坏 / 格式不支持。"""


# ---------------------------------------------------------------- 源文件读取

def read_source(path_str: str) -> dict:
    """读取并校验一个源 WAV, 返回 {rate, samples, duration}。"""
    path = Path(path_str)
    if not path.is_file():
        raise SourceError(f"源文件不存在: {path_str}")
    try:
        wf = wave.open(str(path), "rb")
    except (wave.Error, EOFError) as exc:
        raise SourceError(f"源文件损坏或无法解析: {path_str} ({exc})")
    with wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        if width != 2 or channels != 1:
            raise SourceError(
                "不支持的音频格式: 仅支持16位PCM单声道WAV, "
                f"当前为 {channels} 声道、{width * 8} 位 ({path_str})"
            )
        if not (MIN_SAMPLE_RATE <= rate <= MAX_SAMPLE_RATE):
            raise SourceError(
                "不支持的音频格式: 采样率须在 8kHz-48kHz 之间, "
                f"当前为 {rate}Hz ({path_str})"
            )
        frames = wf.readframes(wf.getnframes())
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":  # WAV 为小端
        samples.byteswap()
    return {"rate": rate, "samples": samples, "duration": len(samples) / rate}


# ---------------------------------------------------------------- 音频处理

def normalize_clip(samples: array.array) -> array.array:
    """峰值归一化到 29491(16 位范围 90% 满量程)。"""
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return array.array("h", samples)
    factor = NORMALIZE_TARGET / peak
    return array.array(
        "h",
        (max(-32768, min(32767, int(round(s * factor)))) for s in samples),
    )


def cut_clip(source: dict, start: float, end: float) -> array.array:
    rate = source["rate"]
    i0 = int(round(start * rate))
    i1 = int(round(end * rate))
    return array.array("h", source["samples"][i0:i1])


# ---------------------------------------------------------------- 报告

def make_item(index: int, raw, status: str, error, output) -> dict:
    return {
        "index": index,
        "source": raw.get("source") if isinstance(raw, dict) else None,
        "status": status,           # success / skipped / error
        "error": error,             # success 时为 None
        "output": output,           # success 时为输出 WAV 路径, 否则 None
    }


# ---------------------------------------------------------------- 主流程

def load_edl(edl_path: str) -> list:
    try:
        text = Path(edl_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise FatalError(f"无法读取剪辑清单 {edl_path}: {exc}")
    try:
        entries = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FatalError(f"剪辑清单不是合法JSON: {edl_path}: {exc}")
    if not isinstance(entries, list):
        raise FatalError("剪辑清单顶层必须是JSON数组")
    return entries


def run(edl_path: str, out_dir: str) -> int:
    """执行批量剪辑, 返回退出码(0 全部成功 / 1 有跳过或出错)。"""
    entries = load_edl(edl_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)  # 输出目录不存在时自动创建
    out_wav_path = out_dir / OUTPUT_WAV_NAME

    results: dict[int, dict] = {}

    # 1) 字段校验: 缺字段的条目记 error 并点名字段
    candidates = []  # (index, entry)
    for i, raw in enumerate(entries):
        if not isinstance(raw, dict):
            results[i] = make_item(i, raw, "error", "条目不是JSON对象", None)
            continue
        missing = [f for f in REQUIRED_FIELDS if f not in raw]
        if missing:
            results[i] = make_item(
                i, raw, "error", "缺少必需字段: " + ", ".join(missing), None)
            continue
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (TypeError, ValueError):
            results[i] = make_item(
                i, raw, "error", "字段 start/end 必须为数字(秒)", None)
            continue
        candidates.append((i, raw, start, end))

    # 2) 读取源文件(按路径缓存); 不存在/损坏/格式不支持 -> SourceError
    sources: dict[str, object] = {}
    for _, raw, _, _ in candidates:
        src = raw["source"]
        if src not in sources:
            try:
                sources[src] = read_source(src)
            except SourceError as exc:
                sources[src] = exc

    # 3) 采样率一致性: 混用不同采样率时整体拒绝, 不写出任何结果音频
    rates = {s["rate"] for s in sources.values() if isinstance(s, dict)}
    if len(rates) > 1:
        detail = ", ".join(
            f"{p}={s['rate']}Hz" for p, s in sources.items()
            if isinstance(s, dict))
        msg = f"采样率不一致, 整体拒绝处理: {detail}"
        print(f"错误: {msg}", file=sys.stderr)
        for i, raw, _, _ in candidates:
            results[i] = make_item(i, raw, "error", msg, None)
        _write_report(out_dir, edl_path, entries, results, None, None)
        return 1

    # 4) 逐条裁剪: 时间范围不合规 -> skipped; 源有问题 -> error; 其余 -> success
    clips = []
    sample_rate = next(iter(rates), None)
    for i, raw, start, end in candidates:
        src = sources[raw["source"]]
        if isinstance(src, SourceError):
            results[i] = make_item(i, raw, "error", str(src), None)
            continue
        duration = src["duration"]
        if start < 0:
            results[i] = make_item(
                i, raw, "skipped", f"起始秒不能为负: {start}", None)
            continue
        if start >= end:
            results[i] = make_item(
                i, raw, "skipped",
                f"起始秒({start})必须小于结束秒({end})", None)
            continue
        if end > duration + 1e-9:
            results[i] = make_item(
                i, raw, "skipped",
                f"结束秒({end})超出源时长({duration:.3f}秒)", None)
            continue
        clip = cut_clip(src, start, end)
        if raw["normalize"]:
            clip = normalize_clip(clip)
        clips.append(clip)
        results[i] = make_item(i, raw, "success", None, str(out_wav_path))

    # 5) 拼接成功片段并写出(同名文件覆盖写入)
    if clips:
        merged = array.array("h")
        for clip in clips:
            merged.extend(clip)
        with wave.open(str(out_wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(merged.tobytes())

    _write_report(out_dir, edl_path, entries, results,
                  str(out_wav_path) if clips else None, sample_rate)
    return 0 if all(r["status"] == "success" for r in results.values()) else 1


def _write_report(out_dir: Path, edl_path: str, entries: list,
                  results: dict, output, sample_rate) -> None:
    report = {
        "edl": str(edl_path),
        "output": output,
        "sample_rate": sample_rate,
        "total": len(entries),
        "success": sum(1 for r in results.values() if r["status"] == "success"),
        "skipped": sum(1 for r in results.values() if r["status"] == "skipped"),
        "error": sum(1 for r in results.values() if r["status"] == "error"),
        "entries": [results[i] for i in sorted(results)],
    }
    report_path = out_dir / REPORT_NAME
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入: {report_path}")
    if output:
        print(f"成片已写入: {output}")
    print(f"合计 {report['total']} 条: "
          f"成功 {report['success']}, 跳过 {report['skipped']}, "
          f"错误 {report['error']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="播客采访录音批量剪辑工具")
    parser.add_argument("edl", help="JSON剪辑清单路径")
    parser.add_argument("output_dir", help="输出目录(不存在时自动创建)")
    args = parser.parse_args(argv)
    try:
        return run(args.edl, args.output_dir)
    except FatalError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
