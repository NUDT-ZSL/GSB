#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频后期粘贴工具（仅使用 Python 标准库）。

功能：
    将源 WAV 的一个片段（从可选源起始点开始到源文件末尾）粘贴到目标 WAV
    的指定插入时间点，生成新的 WAV 文件；同时合并 / 平移两份 JSON 标注，
    保持标注与音频严格同步。

约束：
    - 输入均为单声道、16 位 PCM WAV，且两个文件采样率必须一致。
    - 标注时间单位为秒（浮点数），标签为任意字符串。
    - 所有错误以清晰信息打印到 stderr 并以非零状态码退出，出错时不生成任何
      输出文件。

命令行示例：
    python audio_paste.py \
        --target target.wav --source source.wav \
        --insert-point 2.0 --source-start 0.5 \
        --target-ann target_ann.json --source-ann source_ann.json \
        --output-prefix out/mix
    生成 out/mix_mixed.wav 与 out/mix_annotations.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from dataclasses import dataclass


def _force_utf8_console() -> None:
    """
    Windows 中文控制台默认按 GBK 编码输出，重定向到管道时会导致调用方拿到
    非 UTF-8 字节。统一强制 UTF-8，保证错误信息在任何环境下可读、可断言。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


_force_utf8_console()


class AudioPasteError(Exception):
    """所有可预期的业务错误（参数非法、格式不兼容、文件损坏等）。"""


@dataclass
class WaveData:
    """加载到内存的单声道 16 位 PCM WAV。"""

    framerate: int          # 采样率 Hz
    sampwidth: int          # 采样位宽（字节），固定为 2
    n_frames: int           # 样本帧数（单声道，帧数 = 样本数）
    frames: bytes           # 原始 little-endian 16 位 PCM 字节
    path: str

    @property
    def duration(self) -> float:
        return self.n_frames / self.framerate


# --------------------------------------------------------------------------- #
# WAV 读取 / 写入
# --------------------------------------------------------------------------- #
def load_wav(path: str) -> WaveData:
    """读取并校验单声道 16 位 PCM WAV。"""
    if not os.path.isfile(path):
        raise AudioPasteError(f"WAV 文件不存在: {path}")
    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            comp_type = wf.getcomptype()
            n_frames = wf.getnframes()
            frames = wf.readframes(n_frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise AudioPasteError(f"无法读取 WAV 文件 {path}: {exc}") from exc

    if comp_type != "NONE":
        raise AudioPasteError(
            f"WAV 文件 {path} 不是 PCM 格式（压缩类型: {comp_type}），仅支持 PCM"
        )
    if n_channels != 1:
        raise AudioPasteError(
            f"WAV 文件 {path} 必须为单声道，实际声道数: {n_channels}"
        )
    if sampwidth != 2:
        raise AudioPasteError(
            f"WAV 文件 {path} 必须为 16 位 PCM（位宽 2 字节），"
            f"实际位宽: {sampwidth * 8} 位"
        )
    if framerate <= 0:
        raise AudioPasteError(f"WAV 文件 {path} 采样率非法: {framerate}")
    if len(frames) != n_frames * 2:
        raise AudioPasteError(
            f"WAV 文件 {path} 数据损坏：声明 {n_frames} 帧，"
            f"实际可读 {len(frames) // 2} 帧"
        )
    return WaveData(
        framerate=framerate,
        sampwidth=sampwidth,
        n_frames=n_frames,
        frames=frames,
        path=path,
    )


def write_wav(path: str, framerate: int, frames: bytes) -> None:
    """以与目标文件相同的采样率 / 位深写出单声道 16 位 PCM WAV。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)          # 16 位
        wf.setframerate(framerate)
        wf.writeframes(frames)


# --------------------------------------------------------------------------- #
# 标注读取 / 写入
# --------------------------------------------------------------------------- #
def load_annotations(path: str):
    """
    读取 JSON 标注文件。

    支持两种结构（输出保持与原文件一致的顶层结构）：
      1. 顶层为数组: [{"time": 1.0, "label": "a"}, ...]
      2. 顶层为对象，标注列表放在 "annotations" 键:
         {"annotations": [...], 其他键原样保留}
    每个标注至少包含数值型 "time"（秒）和字符串型 "label"，其余键原样保留。
    返回 (标注列表, 外层字典或 None)。
    """
    if not os.path.isfile(path):
        raise AudioPasteError(f"标注文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise AudioPasteError(f"无法解析标注 JSON 文件 {path}: {exc}") from exc

    wrapper = None
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict) and isinstance(data.get("annotations"), list):
        wrapper = data
        raw_items = data["annotations"]
    else:
        raise AudioPasteError(
            f"标注文件 {path} 格式无效：顶层必须是数组，或为含 "
            f'"annotations" 数组的对象'
        )

    items = []
    for idx, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise AudioPasteError(
                f"标注文件 {path} 第 {idx} 项不是 JSON 对象: {item!r}"
            )
        if "time" not in item or "label" not in item:
            raise AudioPasteError(
                f"标注文件 {path} 第 {idx} 项缺少 'time' 或 'label' 字段"
            )
        if not isinstance(item["time"], (int, float)) or isinstance(
            item["time"], bool
        ):
            raise AudioPasteError(
                f"标注文件 {path} 第 {idx} 项 time 必须是数值"
            )
        if not isinstance(item["label"], str):
            raise AudioPasteError(
                f"标注文件 {path} 第 {idx} 项 label 必须是字符串"
            )
        if item["time"] < 0:
            raise AudioPasteError(
                f"标注文件 {path} 第 {idx} 项 time 不能为负数: {item['time']}"
            )
        items.append(dict(item))  # 浅拷贝，保留额外字段
    return items, wrapper


def write_annotations(path: str, items: list, wrapper) -> None:
    """写出标注 JSON，保持与原目标标注文件一致的顶层结构。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    if wrapper is None:
        payload = items
    else:
        payload = dict(wrapper)
        payload["annotations"] = items
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# 核心业务逻辑
# --------------------------------------------------------------------------- #
def _round9(value: float) -> float:
    """去掉浮点运算尾差（采样率倒数远大于该量级，不损失精度）。"""
    return round(value, 9)


def build_mixed_audio(target: WaveData, source: WaveData,
                      insert_sample: int, src_start_sample: int) -> tuple[bytes, int]:
    """
    拼接音频字节。

    - insert_sample >= 目标帧数时视为追加：输出 = 目标 + 源片段。
    - 否则为插入：输出 = 目标[:插入点] + 源片段 + 目标[插入点:]。
    返回 (混合后的 PCM 字节, 源片段帧数)。
    """
    seg_n = source.n_frames - src_start_sample
    seg_bytes = source.frames[src_start_sample * 2: src_start_sample * 2 + seg_n * 2]

    if insert_sample >= target.n_frames:
        mixed = target.frames + seg_bytes
    else:
        cut = insert_sample * 2
        mixed = target.frames[:cut] + seg_bytes + target.frames[cut:]
    return mixed, seg_n


def build_mixed_annotations(target_ann, source_ann,
                            target_framerate: int,
                            insert_sample: int, target_n_frames: int,
                            src_start_sample: int, seg_n: int) -> list:
    """
    构造混合后的标注（时间单位仍为秒）。

    规则（以样本为精确计算基准）：
      * 追加模式（插入点 >= 目标末尾）：目标标注一律不动；
      * 插入模式：目标标注 time < 插入点（含恰好等于）保持原值；
        time > 插入点的整体后移源片段时长；
      * 源标注仅取落在已复制片段 [src_start, src_start+seg_n] 内的条目，
        新时间 = 插入点 + (源时间 - 源起始点)；
      * 输出按 (time, label) 升序排列。
    """
    append_mode = insert_sample >= target_n_frames
    result = []

    # 1) 目标标注
    for item in target_ann:
        new_item = dict(item)
        if not append_mode:
            t_sample = round(item["time"] * target_framerate)
            if t_sample > insert_sample:
                new_item["time"] = _round9(
                    (t_sample + seg_n) / target_framerate
                )
            # t_sample <= insert_sample：保持原值不变（恰好等于也不动）
        result.append(new_item)

    # 2) 源标注（可选）
    if source_ann:
        seg_start_sec = src_start_sample / target_framerate
        seg_end_sec = (src_start_sample + seg_n) / target_framerate
        for item in source_ann:
            t = item["time"]
            if t < seg_start_sec - 1e-9 or t > seg_end_sec + 1e-9:
                continue  # 不在被复制的片段范围内，丢弃
            new_item = dict(item)
            new_item["time"] = _round9(
                insert_sample / target_framerate
                + (t - seg_start_sec)
            )
            result.append(new_item)

    # 3) 排序：时间升序，时间相同按标签字典序
    result.sort(key=lambda x: (x["time"], x["label"]))
    return result


def paste_audio(target_path: str, source_path: str,
                insert_point: float, source_start: float,
                target_ann_path: str, source_ann_path: str | None,
                output_prefix: str) -> tuple[str, str]:
    """
    完整粘贴流程。成功返回 (wav 输出路径, 标注输出路径)；
    任何校验失败抛出 AudioPasteError，且保证不留下输出文件。
    """
    wav_out = output_prefix + "_mixed.wav"
    ann_out = output_prefix + "_annotations.json"

    # ---- 输入校验（全部在写输出之前完成） ----
    if insert_point < 0:
        raise AudioPasteError(f"插入点不能为负数: {insert_point}")
    if source_start < 0:
        raise AudioPasteError(f"源起始点不能为负数: {source_start}")

    target = load_wav(target_path)
    source = load_wav(source_path)

    if target.framerate != source.framerate:
        raise AudioPasteError(
            f"采样率不一致，拒绝处理：目标文件 {target_path} 为 "
            f"{target.framerate} Hz，源文件 {source_path} 为 "
            f"{source.framerate} Hz"
        )

    rate = target.framerate
    insert_sample = round(insert_point * rate)
    src_start_sample = round(source_start * rate)

    # 需求 8：源起始点超出源文件末尾 -> 报错退出
    if src_start_sample >= source.n_frames:
        raise AudioPasteError(
            f"源起始点 {source_start}s（第 {src_start_sample} 帧）超出源文件"
            f"末尾（共 {source.n_frames} 帧 / {source.duration:.6f}s）"
        )
    # 插入点超出目标末尾 -> 追加到末尾（需求 7）
    if insert_sample > target.n_frames:
        insert_sample = target.n_frames

    target_ann, wrapper = load_annotations(target_ann_path)
    source_ann = None
    if source_ann_path:
        source_ann, _ = load_annotations(source_ann_path)

    # ---- 纯内存计算，确认无误后再落盘 ----
    mixed_frames, seg_n = build_mixed_audio(
        target, source, insert_sample, src_start_sample
    )
    mixed_ann = build_mixed_annotations(
        target_ann, source_ann, rate, insert_sample, target.n_frames,
        src_start_sample, seg_n,
    )

    # 落盘前清理可能存在的旧输出，保证失败场景下不会残留文件
    for p in (wav_out, ann_out):
        if os.path.exists(p):
            os.remove(p)

    write_wav(wav_out, rate, mixed_frames)
    try:
        write_annotations(ann_out, mixed_ann, wrapper)
    except OSError as exc:
        # 标注写失败时回滚已写的 WAV
        if os.path.exists(wav_out):
            os.remove(wav_out)
        raise AudioPasteError(f"标注文件写出失败: {exc}") from exc

    return wav_out, ann_out


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将源 WAV 片段粘贴到目标 WAV 指定时间点，并同步更新 JSON 标注"
    )
    parser.add_argument("--target", required=True, help="目标 WAV 文件路径")
    parser.add_argument("--source", required=True, help="源 WAV 文件路径")
    parser.add_argument(
        "--insert-point", type=float, required=True,
        help="目标文件上的插入时间点（秒）"
    )
    parser.add_argument(
        "--source-start", type=float, default=0.0,
        help="源文件片段起始时间（秒），可选，默认 0"
    )
    parser.add_argument(
        "--target-ann", required=True, help="目标标注 JSON 文件路径"
    )
    parser.add_argument(
        "--source-ann", default=None, help="源标注 JSON 文件路径（可选）"
    )
    parser.add_argument(
        "--output-prefix", required=True,
        help="输出前缀，生成 <前缀>_mixed.wav 与 <前缀>_annotations.json"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        wav_out, ann_out = paste_audio(
            target_path=args.target,
            source_path=args.source,
            insert_point=args.insert_point,
            source_start=args.source_start,
            target_ann_path=args.target_ann,
            source_ann_path=args.source_ann,
            output_prefix=args.output_prefix,
        )
    except AudioPasteError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    print(f"完成: {wav_out}")
    print(f"完成: {ann_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
