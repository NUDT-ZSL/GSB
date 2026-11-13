#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
podcast_concat.py — 播客单集拼接工具（仅使用 Python 标准库，3.10+）

功能：
  按清单 JSON 顺序拼接 单声道 / 16bit / 44100Hz PCM WAV 片段，
  相邻片段间插入 0.5 秒静音；计算各片段 RMS 并标注音量过低片段；
  备份成功片段到 backup/ 目录；输出 JSON 报告。

用法：
  python podcast_concat.py 清单.json [-o 输出.wav] [-r 报告.json] [-b 备份目录]

清单格式：
  {
    "segments": [
      {"path": "intro.wav", "title": "开场", "offset": 0.0},   // offset 可选，单位秒，跳过片段开头
      ...
    ]
  }
  相对路径相对于清单文件所在目录解析。

退出码：
  0 — 正常完成（报告 status 为 complete 或 partial）
  2 — 清单格式错误（输出含“清单格式错误”）
"""
from __future__ import annotations

import argparse
import array
import json
import math
import shutil
import sys
import wave
from pathlib import Path

SAMPLE_RATE = 44100
SAMPLE_WIDTH = 2          # 16 bit
CHANNELS = 1              # 单声道
SILENCE_SECONDS = 0.5
SILENCE_FRAMES = int(SAMPLE_RATE * SILENCE_SECONDS)   # 22050
SILENCE_BYTES = SILENCE_FRAMES * SAMPLE_WIDTH * CHANNELS

# 音量过低判定：片段 RMS 低于“其余有效片段 RMS 均值”的 50% 时标注。
# 该规则与验收用例一致：RMS 1000 与 400 → 400 < 1000*50%，标注较低者；
# RMS 1000 与 600 → 600 不小于 1000*50%，不标注。仅一个有效片段时不标注。
LOW_VOLUME_RATIO = 0.5

EXIT_OK = 0
EXIT_MANIFEST_ERROR = 2


class ManifestError(Exception):
    """清单结构不合法。"""


class SegmentError(Exception):
    """片段不可用，message 作为 skipped 原因写入报告。"""


# ---------------------------------------------------------------- 清单

def load_manifest(manifest_path: Path) -> list[dict]:
    """读取并校验清单，不合法时抛出 ManifestError。"""
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError(f"清单文件不存在：{manifest_path}")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"清单文件无法读取：{exc}")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"JSON 解析失败：{exc}")

    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), list):
        raise ManifestError("顶层必须是包含 segments 数组的 JSON 对象")
    if not manifest["segments"]:
        raise ManifestError("segments 数组为空")

    for i, item in enumerate(manifest["segments"]):
        if not isinstance(item, dict):
            raise ManifestError(f"segments[{i}] 必须是对象")
        if not isinstance(item.get("path"), str) or not item["path"].strip():
            raise ManifestError(f"segments[{i}] 缺少有效的 path 字符串")
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            raise ManifestError(f"segments[{i}] 缺少有效的 title 字符串")
        offset = item.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset < 0:
            raise ManifestError(f"segments[{i}].offset 必须是非负数字（秒）")
    return manifest["segments"]


# ---------------------------------------------------------------- 片段读取

def compute_rms(raw: bytes) -> float:
    """16bit 小端 PCM 样本的 RMS。"""
    if not raw:
        return 0.0
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":          # WAV 固定小端
        samples.byteswap()
    n = len(samples)
    return math.sqrt(sum(s * s for s in samples) / n)


def read_segment(path: Path, offset: float) -> tuple[bytes, int, float]:
    """
    读取并校验一个 WAV 片段。
    返回 (PCM 字节, 帧数, RMS)；不可用时抛出 SegmentError（原因已分类）。
    """
    if not path.exists():
        raise SegmentError("文件缺失：路径不存在")
    if not path.is_file():
        raise SegmentError("文件缺失：路径不是普通文件")

    try:
        wf = wave.open(str(path), "rb")
    except (wave.Error, EOFError, OSError) as exc:
        detail = str(exc) or "不是有效的 WAV 文件或内容为空"
        raise SegmentError(f"文件损坏：无法解析 WAV（{detail}）")

    with wf:
        nch, sw = wf.getnchannels(), wf.getsampwidth()
        fr, comptype = wf.getframerate(), wf.getcomptype()
        problems = []
        if nch != CHANNELS:
            problems.append(f"声道数为 {nch}（要求单声道）")
        if sw != SAMPLE_WIDTH:
            problems.append(f"采样位深为 {sw * 8}bit（要求 16bit）")
        if fr != SAMPLE_RATE:
            problems.append(f"采样率为 {fr}Hz（要求 44100Hz）")
        if comptype != "NONE":
            problems.append(f"压缩类型为 {comptype}（要求未压缩 PCM）")
        if problems:
            raise SegmentError("格式不符：" + "；".join(problems))

        nframes = wf.getnframes()
        try:
            raw = wf.readframes(nframes)
        except (wave.Error, EOFError, OSError) as exc:
            detail = str(exc) or "音频数据读取失败"
            raise SegmentError(f"文件损坏：读取音频数据失败（{detail}）")

    expected = nframes * SAMPLE_WIDTH * CHANNELS
    if len(raw) != expected:
        raise SegmentError(
            f"文件损坏：音频数据长度不符（声明 {expected} 字节，实际 {len(raw)} 字节）")

    if offset > 0:
        skip = int(offset * SAMPLE_RATE) * SAMPLE_WIDTH * CHANNELS
        if skip >= len(raw):
            raise SegmentError(f"offset {offset}s 超出片段时长，跳过该片段")
        raw = raw[skip:]

    nframes_out = len(raw) // (SAMPLE_WIDTH * CHANNELS)
    return raw, nframes_out, compute_rms(raw)


# ---------------------------------------------------------------- 主流程

def process(manifest_path: Path, output_path: Path, report_path: Path,
            backup_dir: Path) -> dict:
    segments = load_manifest(manifest_path)
    base_dir = manifest_path.resolve().parent

    success: list[dict] = []
    skipped: list[dict] = []
    chunks: list[bytes] = []
    total_frames = 0

    for i, item in enumerate(segments, 1):
        title, raw_path = item["title"], item["path"]
        seg_path = Path(raw_path)
        if not seg_path.is_absolute():
            seg_path = base_dir / seg_path
        offset = float(item.get("offset", 0))
        print(f"[{i}/{len(segments)}] {title} ({raw_path}) ... ", end="", flush=True)
        try:
            raw, nframes, rms = read_segment(seg_path, offset)
        except SegmentError as exc:
            reason = str(exc)
            skipped.append({"path": raw_path, "title": title, "reason": reason})
            print(f"跳过（{reason}）")
            continue

        if success:  # 相邻有效片段之间插入 0.5 秒静音
            chunks.append(b"\x00" * SILENCE_BYTES)
            total_frames += SILENCE_FRAMES
        entry = {
            "title": title,
            "path": raw_path,
            "rms": round(rms, 2),
            "start_frame": total_frames,
            "start_seconds": round(total_frames / SAMPLE_RATE, 3),
            "duration_seconds": round(nframes / SAMPLE_RATE, 3),
            "warnings": [],
            "_source": seg_path,   # 内部字段，写报告前移除
        }
        success.append(entry)
        chunks.append(raw)
        total_frames += nframes
        print(f"OK（{nframes} 帧，RMS={rms:.1f}）")

    # ---- 音量过低标注（不修改音频）----
    rms_values = [e["rms"] for e in success]
    for i, entry in enumerate(success):
        others = [v for j, v in enumerate(rms_values) if j != i]
        if others and entry["rms"] < LOW_VOLUME_RATIO * (sum(others) / len(others)):
            entry["warnings"].append("音量过低")
            print(f"提示：片段「{entry['title']}」RMS={entry['rms']}，低于其余有效片段"
                  f"均值的 50%，标注为音量过低")

    # ---- 写出拼接音频 ----
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as out:
        out.setnchannels(CHANNELS)
        out.setsampwidth(SAMPLE_WIDTH)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(b"".join(chunks))
    print(f"拼接输出：{output_path}（{total_frames} 帧，"
          f"{total_frames / SAMPLE_RATE:.2f} 秒）")

    # ---- 备份成功片段（文件名不变，仅复制有效片段）----
    backed_up = []
    if success:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for entry in success:
            dst = backup_dir / Path(entry["_source"]).name
            shutil.copy2(entry["_source"], dst)
            backed_up.append(dst.name)
        print(f"备份完成：{backup_dir}（{len(backed_up)} 个文件）")

    # ---- 报告 ----
    for entry in success:
        del entry["_source"]
    report = {
        "status": "complete" if not skipped else "partial",
        "output": str(output_path),
        "backup_dir": str(backup_dir),
        "silence_seconds": SILENCE_SECONDS,
        "sample_rate": SAMPLE_RATE,
        "total_frames": total_frames,
        "duration_seconds": round(total_frames / SAMPLE_RATE, 3),
        "success": success,
        "skipped": skipped,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"报告已写出：{report_path}")
    print(f"状态：{report['status']}（成功 {len(success)}，跳过 {len(skipped)}）")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="播客单集拼接工具（仅标准库）")
    parser.add_argument("manifest", help="清单 JSON 路径")
    parser.add_argument("-o", "--output", default="episode.wav", help="拼接输出 WAV 路径")
    parser.add_argument("-r", "--report", default="report.json", help="JSON 报告输出路径")
    parser.add_argument("-b", "--backup", default="backup", help="备份目录")
    args = parser.parse_args(argv)

    try:
        process(Path(args.manifest), Path(args.output),
                Path(args.report), Path(args.backup))
    except ManifestError as exc:
        print(f"清单格式错误：{exc}", file=sys.stderr)
        return EXIT_MANIFEST_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
