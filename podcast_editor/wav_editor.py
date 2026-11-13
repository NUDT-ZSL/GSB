# -*- coding: utf-8 -*-
"""播客 WAV 裁剪拼接工具。

从 JSON 配置读取片段列表，按顺序从多个 PCM WAV 源文件中裁剪片段并拼接为
单个节目 WAV。输出 WAV 的采样率 / 声道数 / 采样宽度以**首个可用片段**的
参数为准；参数不一致、源文件缺失 / 不支持、区间越界等情况分别处理，并生成
JSON 报告（完整 / 部分完成 / 失败三类，按配置顺序排列）。

仅依赖 Python 标准库（wave / json / tempfile 等），不使用第三方包。

命令行用法：
    python -m podcast_editor.wav_editor --config config.json \
        --output output/program.wav --report output/report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


class ConfigError(Exception):
    """配置文件本身非法（结构错误、缺少必要字段、片段列表为空等）。"""


@dataclass
class SegmentRecord:
    """单个片段的处理结果，最终写入 JSON 报告。"""

    index: int
    source: str
    start_sec: float
    end_sec: float
    status: str  # "complete" | "partial" | "failed"
    reason: str
    output_frames: int = 0


@dataclass
class BuildResult:
    status: str  # "success" | "partial" | "failed"
    output_path: Optional[str]
    report_path: str
    reference_params: Optional[Dict[str, int]]
    segments: List[SegmentRecord] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 配置读取与校验
# --------------------------------------------------------------------------- #
def load_config(config_path: str) -> List[Dict[str, Any]]:
    """读取并校验 JSON 配置，返回片段字典列表。

    配置格式：
        {
          "clips": [
            {"source": "path/to/a.wav", "start": 0.0, "end": 3.5},
            ...
          ]
        }
    配置非法（文件不存在 / JSON 语法错 / 缺少 clips / clips 为空 /
    片段缺字段 / 时间非法）时抛 ConfigError，由 main 统一以退出码 2 结束。
    """
    if not os.path.isfile(config_path):
        raise ConfigError("配置文件不存在: %s" % config_path)

    try:
        # utf-8-sig 同时兼容带/不带 BOM 的 UTF-8（Windows 记事本、PS5.1 默认带 BOM）
        with open(config_path, "r", encoding="utf-8-sig") as fh:
            config = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError("配置文件不是合法 JSON: %s" % exc) from exc

    if not isinstance(config, dict) or "clips" not in config:
        raise ConfigError("配置缺少必要字段 'clips'（片段列表）")

    clips = config["clips"]
    if not isinstance(clips, list):
        raise ConfigError("字段 'clips' 必须是片段列表")
    if len(clips) == 0:
        raise ConfigError("片段列表为空，至少需要一个片段")

    validated: List[Dict[str, Any]] = []
    for i, clip in enumerate(clips):
        if not isinstance(clip, dict):
            raise ConfigError("片段 #%d 必须是 JSON 对象" % i)
        for key in ("source", "start", "end"):
            if key not in clip:
                raise ConfigError("片段 #%d 缺少必要字段 '%s'" % (i, key))
        source = clip["source"]
        start = clip["start"]
        end = clip["end"]
        if not isinstance(source, str) or not source.strip():
            raise ConfigError("片段 #%d 的 'source' 必须是非空字符串" % i)
        # bool 是 int 的子类，显式排除 True/False 这类非法时间值
        if isinstance(start, bool) or isinstance(end, bool) or \
                not isinstance(start, (int, float)) or \
                not isinstance(end, (int, float)):
            raise ConfigError("片段 #%d 的 'start'/'end' 必须是数字（秒）" % i)
        if start < 0:
            raise ConfigError("片段 #%d 的 'start' 不能为负数" % i)
        if end <= start:
            raise ConfigError(
                "片段 #%d 的 'end'(%.3f) 必须大于 'start'(%.3f)"
                % (i, float(end), float(start))
            )
        validated.append(
            {"source": source, "start": float(start), "end": float(end)}
        )
    return validated


# --------------------------------------------------------------------------- #
# 核心处理
# --------------------------------------------------------------------------- #
def _read_wave_params(path: str) -> Tuple[wave._wave_params, int]:
    """打开 WAV 并返回 (参数对象, 总帧数)。

    仅支持未压缩 PCM WAV（wave 模块可解析的格式）。文件缺失、不是 WAV、
    压缩格式等情况由调用方捕获异常并记为片段失败。
    """
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        if params.comptype != "NONE":
            raise wave.Error("不支持的压缩格式: %s" % params.comptype)
        n_frames = wf.getnframes()
    return params, n_frames


def build_program(
    clips: List[Dict[str, Any]],
    output_path: str,
    report_path: str,
    base_dir: str,
) -> BuildResult:
    """按配置顺序裁剪并拼接片段，写出输出 WAV 与 JSON 报告。"""
    records: List[SegmentRecord] = []
    # 首个“可用片段”（成功贡献音频数据的片段）确定输出参数
    ref: Optional[wave._wave_params] = None
    total_output_frames = 0

    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)
    report_dir = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(report_dir, exist_ok=True)

    # 先写临时文件，全部处理完再原子替换，避免半成品输出
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".wav", prefix=".program-", dir=out_dir
    )
    os.close(tmp_fd)
    writer: Optional[wave.Wave_write] = None

    try:
        for i, clip in enumerate(clips):
            source_raw = clip["source"]
            start_sec = clip["start"]
            end_sec = clip["end"]
            # 配置中的相对路径相对配置文件所在目录解析
            source_abs = (
                source_raw
                if os.path.isabs(source_raw)
                else os.path.normpath(os.path.join(base_dir, source_raw))
            )
            record = SegmentRecord(
                index=i,
                source=source_raw,
                start_sec=start_sec,
                end_sec=end_sec,
                status="failed",
                reason="",
            )

            # 1) 源文件缺失 / 格式不支持 ------------------------------- #
            if not os.path.isfile(source_abs):
                record.reason = "源文件缺失: %s" % source_abs
                records.append(record)
                continue
            try:
                params, src_total = _read_wave_params(source_abs)
            except (wave.Error, EOFError, Exception) as exc:  # noqa: BLE001
                # wave 对损坏文件可能抛 wave.Error / EOFError 等，统一归类
                record.reason = "源文件格式不支持或已损坏: %s" % exc
                records.append(record)
                continue

            # 2) 参数一致性（首个可用片段确定输出参数） ----------------- #
            if ref is None:
                ref = params
                writer = wave.open(tmp_path, "wb")
                writer.setparams(ref)
            elif (
                params.nchannels != ref.nchannels
                or params.sampwidth != ref.sampwidth
                or params.framerate != ref.framerate
            ):
                record.reason = (
                    "音频参数与首个可用片段不一致: "
                    "源参数为 %d 声道/%d Hz/%d 字节, 输出要求 %d 声道/%d Hz/%d 字节"
                    % (
                        params.nchannels,
                        params.framerate,
                        params.sampwidth,
                        ref.nchannels,
                        ref.framerate,
                        ref.sampwidth,
                    )
                )
                records.append(record)
                continue

            # 3) 时间区间换算为帧并处理越界 ----------------------------- #
            start_frame = int(round(start_sec * params.framerate))
            end_frame = int(round(end_sec * params.framerate))
            if start_frame >= src_total:
                record.reason = (
                    "起始秒 %.3f 已超出源文件时长 %.3f 秒，无可输出音频"
                    % (start_sec, src_total / params.framerate)
                )
                records.append(record)
                continue
            if start_frame < 0:
                start_frame = 0

            clipped = False
            clip_reason = ""
            if end_frame > src_total:
                clipped = True
                clip_reason = (
                    "结束秒 %.3f 超过源文件时长 %.3f 秒，已截取到源文件末尾"
                    % (end_sec, src_total / params.framerate)
                )
                end_frame = src_total
            if end_frame <= start_frame:
                record.reason = (
                    "裁剪区间为空（结束帧 %d <= 起始帧 %d）"
                    % (end_frame, start_frame)
                )
                records.append(record)
                continue

            # 4) 读取并写出帧数据 --------------------------------------- #
            try:
                with wave.open(source_abs, "rb") as reader:
                    reader.setpos(start_frame)
                    remaining = end_frame - start_frame
                    while remaining > 0:
                        chunk = reader.readframes(min(remaining, 1_048_576))
                        if not chunk:
                            break
                        writer.writeframes(chunk)
                        got = len(chunk) // (params.sampwidth * params.nchannels)
                        remaining -= got
                    actual_frames = (end_frame - start_frame) - remaining
            except (wave.Error, OSError) as exc:
                record.reason = "读取源文件音频数据失败: %s" % exc
                records.append(record)
                continue

            record.output_frames = actual_frames
            total_output_frames += actual_frames
            if clipped:
                record.status = "partial"
                record.reason = clip_reason
            else:
                record.status = "complete"
                record.reason = (
                    "裁剪成功，输出 %.3f 秒（%d 帧）"
                    % (
                        actual_frames / params.framerate,
                        actual_frames,
                    )
                )
            records.append(record)
    finally:
        if writer is not None:
            writer.close()

    # 5) 汇总并写报告 --------------------------------------------------- #
    complete = [r for r in records if r.status == "complete"]
    partial = [r for r in records if r.status == "partial"]
    failed = [r for r in records if r.status == "failed"]

    if ref is None or total_output_frames == 0:
        # 全部片段失败：不生成输出 WAV
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists(output_path):
            os.remove(output_path)
        status = "failed"
        output_actual: Optional[str] = None
        ref_dict = None
        exit_nonzero = True
    else:
        os.replace(tmp_path, output_path)
        output_actual = output_path
        ref_dict = {
            "channels": ref.nchannels,
            "sample_rate": ref.framerate,
            "sample_width_bytes": ref.sampwidth,
        }
        status = "success" if not failed else "partial"
        exit_nonzero = False

    def entry(r: SegmentRecord) -> Dict[str, Any]:
        return {
            "index": r.index,
            "source": r.source,
            "start_sec": r.start_sec,
            "end_sec": r.end_sec,
            "status": r.status,
            "reason": r.reason,
            "output_frames": r.output_frames,
            "output_duration_sec": (
                round(r.output_frames / ref.framerate, 6)
                if ref is not None and r.output_frames
                else 0.0
            ),
        }

    report = {
        "status": status,
        "output_wav": output_actual,
        "output_params": ref_dict,
        "total_output_frames": total_output_frames if ref is not None else 0,
        "total_output_duration_sec": (
            round(total_output_frames / ref.framerate, 6)
            if ref is not None
            else 0.0
        ),
        "counts": {
            "complete": len(complete),
            "partial": len(partial),
            "failed": len(failed),
            "total": len(records),
        },
        # 三个列表均按配置顺序（index 升序）排列，每个条目原因非空
        "complete_segments": [entry(r) for r in complete],
        "partial_segments": [entry(r) for r in partial],
        "failed_segments": [entry(r) for r in failed],
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    return BuildResult(
        status=status,
        output_path=output_actual,
        report_path=report_path,
        reference_params=ref_dict,
        segments=records,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="按 JSON 配置裁剪并拼接 PCM WAV 片段，输出节目 WAV 与 JSON 报告"
    )
    parser.add_argument("--config", required=True, help="JSON 配置文件路径")
    parser.add_argument("--output", required=True, help="输出 WAV 路径")
    parser.add_argument("--report", required=True, help="输出 JSON 报告路径")
    args = parser.parse_args(argv)

    # Windows 控制台默认 GBK，强制 UTF-8 输出保证中文原因可读
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    try:
        clips = load_config(args.config)
    except ConfigError as exc:
        print("配置错误: %s" % exc, file=sys.stderr)
        return 2

    base_dir = os.path.dirname(os.path.abspath(args.config))
    result = build_program(clips, args.output, args.report, base_dir)

    print("处理状态: %s" % result.status)
    print("报告文件: %s" % result.report_path)
    if result.output_path:
        print("输出 WAV: %s" % result.output_path)
        print("输出参数: %s" % result.reference_params)
    else:
        print("全部片段失败，未生成输出 WAV")

    # 全部失败 -> 非零退出码；部分失败但有可用输出 -> 0
    return 3 if result.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
