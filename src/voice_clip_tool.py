# -*- coding: utf-8 -*-
"""角色配音批处理工具（仅使用 Python 标准库）。

功能：
1. 读取单声道 16 位 PCM WAV 与 JSON 片段配置（id / start_ms / end_ms，按配置顺序处理）。
2. 区间越界或起止不合法的片段标记“区间无效”，不参与裁剪与合并。
3. 有效片段内所有采样绝对值均低于 5 时标记“静音”，不参与合并。
4. 非静音片段按 RMS 计算响度，施加增益朝目标 -20 dBFS 归一化，单片段增益上限 +20 dB。
5. 所需增益超过 +20 dB 时按 +20 dB 施加，标记“增益受限”。
6. 增益未受限并达到目标的非静音片段标记“已平衡”。
7. 满幅 90% 以上采样占比超过 1% 时标记“可能削波”，仍完成响度平衡并参与合并。
8. 保留片段按配置顺序拼接，片段间插入 50 ms 静音，输出单声道 16 位 PCM WAV。
9. 输出 UTF-8 文本报告，逐片段给出 id、状态、原始 RMS(dBFS)、增益(dB) 与说明。

退出码：
  0 正常（含存在无效/静音片段的情况）
  1 配置文件错误（JSON 不可解析 / 缺必要字段 / 路径不存在等）
  2 不支持的音频格式（WAV 不存在或不是单声道 16 位 PCM）
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import wave

# ----------------------------- 业务常量 -----------------------------

TARGET_DBFS = -20.0        # 目标响度
MAX_GAIN_DB = 20.0         # 单片段最大正向增益
SILENCE_ABS_THRESHOLD = 5  # 静音判定：所有采样 |sample| < 5
CLIP_RATIO = 0.90          # 削波判定：采样绝对值 >= 满幅 * 0.90
CLIP_RATIO_THRESHOLD = 0.01  # 且占比 > 1%
GAP_MS = 50                # 片段间静音时长
FULL_SCALE = 32768.0       # 16 位有符号整数满幅（-32768 的绝对值）
CLIP_ABS_LEVEL = int(round(32767 * CLIP_RATIO))  # 29490

# 报告状态（取值固定，便于自动化校验）
STATUS_INVALID = "区间无效"
STATUS_SILENT = "静音"
STATUS_GAIN_LIMITED = "增益受限"
STATUS_POSSIBLE_CLIP = "可能削波"
STATUS_BALANCED = "已平衡"


class ConfigError(Exception):
    """配置文件错误，退出码 1。"""


class AudioFormatError(Exception):
    """不支持的音频格式，退出码 2。"""


# ----------------------------- 输入读取 -----------------------------

def load_config(config_path: str) -> list[dict]:
    """读取并校验 JSON 配置，返回片段列表（保持配置顺序）。

    配置整体不可解析或缺少必要字段时抛出 ConfigError。
    """
    if not os.path.isfile(config_path):
        raise ConfigError("配置文件错误: 配置文件不存在: %s" % config_path)
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError("配置文件错误: JSON 无法解析: %s" % exc) from exc
    except OSError as exc:
        raise ConfigError("配置文件错误: 无法读取配置: %s" % exc) from exc

    if isinstance(data, dict) and "segments" in data:
        segments = data["segments"]
    else:
        segments = data
    if not isinstance(segments, list) or not segments:
        raise ConfigError("配置文件错误: 片段列表必须为非空数组")

    cleaned: list[dict] = []
    required = ("id", "start_ms", "end_ms")
    for index, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise ConfigError("配置文件错误: 第 %d 个片段不是对象" % index)
        missing = [k for k in required if k not in seg]
        if missing:
            raise ConfigError(
                "配置文件错误: 第 %d 个片段缺少必要字段 %s" % (index, ",".join(missing))
            )
        try:
            start_ms = float(seg["start_ms"])
            end_ms = float(seg["end_ms"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "配置文件错误: 第 %d 个片段的毫秒值不是数字" % index
            ) from exc
        if not math.isfinite(start_ms) or not math.isfinite(end_ms):
            raise ConfigError(
                "配置文件错误: 第 %d 个片段的毫秒值必须为有限数" % index
            )
        cleaned.append(
            {
                "id": str(seg["id"]),
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    return cleaned


def load_mono_16bit_wav(wav_path: str) -> tuple[list[int], int]:
    """读取单声道 16 位 PCM WAV，返回 (采样列表, 采样率)。

    文件不存在或格式不符时抛出 AudioFormatError。
    """
    if not os.path.isfile(wav_path):
        raise AudioFormatError("不支持的音频格式: WAV 文件不存在: %s" % wav_path)
    try:
        with wave.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioFormatError(
            "不支持的音频格式: 无法解析 WAV 文件: %s" % exc
        ) from exc

    if n_channels != 1 or sample_width != 2:
        raise AudioFormatError(
            "不支持的音频格式: 仅支持单声道 16 位 PCM WAV，"
            "当前为 %d 声道、%d 位 (文件: %s)"
            % (n_channels, sample_width * 8, wav_path)
        )

    samples = list(struct.unpack("<%dh" % n_frames, raw))
    return samples, sample_rate


# ----------------------------- 分析与处理 -----------------------------

def rms_dbfs(samples: list[int]) -> tuple[float | None, float]:
    """返回 (RMS 分贝值, 线性 RMS)。全静音（零能量）时分贝值为 None。"""
    if not samples:
        return None, 0.0
    total_sq = 0.0
    for s in samples:
        total_sq += s * s
    linear = math.sqrt(total_sq / len(samples))
    if linear <= 0.0:
        return None, 0.0
    return 20.0 * math.log10(linear / FULL_SCALE), linear


def is_silent(samples: list[int]) -> bool:
    """所有采样绝对值均低于 5 即静音。"""
    return all(abs(s) < SILENCE_ABS_THRESHOLD for s in samples)


def clip_ratio(samples: list[int]) -> float:
    """达到满幅 90% 的采样占比。"""
    if not samples:
        return 0.0
    hit = sum(1 for s in samples if abs(s) >= CLIP_ABS_LEVEL)
    return hit / len(samples)


def apply_gain(samples: list[int], gain_db: float) -> list[int]:
    """按分贝增益缩放并钳制到 16 位有符号范围。"""
    factor = 10.0 ** (gain_db / 20.0)
    out: list[int] = []
    for s in samples:
        v = int(round(s * factor))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(v)
    return out


def process_segment(
    samples: list[int], sample_rate: int, seg: dict
) -> dict:
    """处理单个片段，返回结果记录。"""
    total_frames = len(samples)
    duration_ms = total_frames / sample_rate * 1000.0

    start_idx = int(round(seg["start_ms"] / 1000.0 * sample_rate))
    end_idx = int(round(seg["end_ms"] / 1000.0 * sample_rate))

    result: dict = {
        "id": seg["id"],
        "start_ms": seg["start_ms"],
        "end_ms": seg["end_ms"],
        "status": None,
        "original_rms_dbfs": None,
        "gain_db": None,
        "output_samples": None,
        "input_frames": 0,
        "note": "",
    }

    # 区间合法性：起止为正、start < end、不得越过音频时长
    if (
        seg["start_ms"] < 0
        or seg["end_ms"] < 0
        or start_idx >= end_idx
        or start_idx < 0
        or end_idx > total_frames
    ):
        result["status"] = STATUS_INVALID
        result["note"] = (
            "区间超出音频时长(%.3f ms)或起止不合法，已跳过" % duration_ms
        )
        return result

    clip_samples = samples[start_idx:end_idx]
    result["input_frames"] = len(clip_samples)

    # 静音判定优先，静音片段不计算响度、不进入合并
    if is_silent(clip_samples):
        result["status"] = STATUS_SILENT
        result["note"] = "片段内所有采样绝对值均低于 %d，判定为静音，不参与合并" % (
            SILENCE_ABS_THRESHOLD
        )
        return result

    original_dbfs, _ = rms_dbfs(clip_samples)
    result["original_rms_dbfs"] = original_dbfs

    possible_clip = clip_ratio(clip_samples) > CLIP_RATIO_THRESHOLD

    # 朝目标响度归一化；只限制正向增益，负向衰减不受限
    required_gain = TARGET_DBFS - original_dbfs
    limited = required_gain > MAX_GAIN_DB
    gain_db = min(required_gain, MAX_GAIN_DB)
    result["gain_db"] = gain_db

    result["output_samples"] = apply_gain(clip_samples, gain_db)

    if limited:
        result["status"] = STATUS_GAIN_LIMITED
        result["note"] = (
            "所需增益 %.2f dB 超过上限，实际施加 +%.1f dB，未达到目标 %.1f dBFS"
            % (required_gain, gain_db, TARGET_DBFS)
        )
    elif possible_clip:
        result["status"] = STATUS_POSSIBLE_CLIP
        result["note"] = (
            "满幅 %d%% 以上采样占比超过 %.2f%%，仍完成响度平衡并参与合并"
            % (int(CLIP_RATIO * 100), CLIP_RATIO_THRESHOLD * 100)
        )
    else:
        result["status"] = STATUS_BALANCED
        result["note"] = "已施加 %.2f dB 增益，响度平衡至目标 %.1f dBFS" % (
            gain_db,
            TARGET_DBFS,
        )
    return result


# ----------------------------- 输出 -----------------------------

def merge_pieces(pieces: list[list[int]], sample_rate: int) -> list[int]:
    """按配置顺序拼接保留片段，片段间插入 50 ms 静音。"""
    gap_frames = int(round(GAP_MS / 1000.0 * sample_rate))
    merged: list[int] = []
    for i, piece in enumerate(pieces):
        if i > 0:
            merged.extend([0] * gap_frames)
        merged.extend(piece)
    return merged


def write_wav(path: str, samples: list[int], sample_rate: int) -> None:
    """写单声道 16 位 PCM WAV，分块打包避免大内存分配。"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        chunk = 65536
        for i in range(0, len(samples), chunk):
            wf.writeframes(struct.pack("<%dh" % len(samples[i:i + chunk]),
                                       *samples[i:i + chunk]))


def _fmt_rms(value: float | None) -> str:
    return "null" if value is None else "%.2f" % value


def _fmt_gain(value: float | None) -> str:
    return "null" if value is None else "%.1f" % value


def write_report(
    path: str,
    results: list[dict],
    wav_path: str,
    sample_rate: int,
    total_input_frames: int,
    retained_count: int,
    gap_frames: int,
    merged_frames: int,
) -> None:
    """写 UTF-8 文本报告（无 BOM，LF 换行）。"""
    lines: list[str] = []
    lines.append("# 角色配音批处理报告")
    lines.append("源 WAV: %s" % wav_path)
    lines.append("采样率: %d Hz，单声道 16 位 PCM" % sample_rate)
    lines.append(
        "源时长: %.3f ms；保留片段: %d 个；段间静音: %d ms"
        % (total_input_frames / sample_rate * 1000.0,
           retained_count, GAP_MS)
    )
    lines.append(
        "合并输出时长: %.3f ms（保留片段时长合计 + %d ms × (片段数-1)）"
        % (
            merged_frames / sample_rate * 1000.0,
            GAP_MS,
        )
    )
    lines.append("")
    header = "[序号] id | 状态 | 原始RMS(dBFS) | 增益(dB) | 说明"
    lines.append(header)
    lines.append("-" * len(header.encode("gbk", errors="ignore")))
    for i, r in enumerate(results, 1):
        lines.append(
            "[%d] %s | %s | %s | %s | %s"
            % (
                i,
                r["id"],
                r["status"],
                _fmt_rms(r["original_rms_dbfs"]),
                _fmt_gain(r["gain_db"]),
                r["note"],
            )
        )
    lines.append("")
    lines.append(
        "状态说明: %s=跳过不合并；%s=不合并；%s=按+%.1fdB封顶；%s=仍参与合并；%s=达到%.1fdBFS目标"
        % (
            STATUS_INVALID,
            STATUS_SILENT,
            STATUS_GAIN_LIMITED,
            MAX_GAIN_DB,
            STATUS_POSSIBLE_CLIP,
            STATUS_BALANCED,
            TARGET_DBFS,
        )
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


# ----------------------------- 主流程 -----------------------------

def run(wav_path: str, config_path: str, out_wav: str, out_report: str) -> int:
    # 配置与音频先全部读入并校验，任何前置错误都不会生成输出文件
    segments = load_config(config_path)
    samples, sample_rate = load_mono_16bit_wav(wav_path)

    results = [process_segment(samples, sample_rate, seg) for seg in segments]

    pieces = [r["output_samples"] for r in results if r["output_samples"]]
    merged = merge_pieces(pieces, sample_rate)

    out_dir = os.path.dirname(os.path.abspath(out_wav))
    report_dir = os.path.dirname(os.path.abspath(out_report))
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(report_dir, exist_ok=True)

    write_wav(out_wav, merged, sample_rate)

    gap_frames = int(round(GAP_MS / 1000.0 * sample_rate))
    retained_frames = sum(len(p) for p in pieces)
    write_report(
        out_report,
        results,
        wav_path,
        sample_rate,
        len(samples),
        len(pieces),
        gap_frames,
        len(merged),
    )
    return 0


def _reconfigure_streams() -> None:
    """Windows 控制台/重定向默认可能为 GBK，统一为 UTF-8，保证中文提示可复现。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _reconfigure_streams()
    parser = argparse.ArgumentParser(
        description="单声道 16 位 PCM 角色配音裁剪、响度平衡与合并工具（仅标准库）"
    )
    parser.add_argument("--wav", required=True, help="输入单声道 16 位 PCM WAV 路径")
    parser.add_argument("--config", required=True, help="JSON 片段配置路径")
    parser.add_argument("--out-wav", required=True, help="输出合并 WAV 路径")
    parser.add_argument("--out-report", required=True, help="输出 UTF-8 报告路径")
    args = parser.parse_args(argv)

    try:
        return run(args.wav, args.config, args.out_wav, args.out_report)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except AudioFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
