"""命令行入口与全部业务逻辑，仅使用 Python 标准库。

用法::

    python -m wav_editor <source.wav> <plan.json> <output.wav>

退出码：
    0  正常完成（即使全部片段被跳过，也会生成空 WAV）
    2  参数缺失 / 文件不可读 / 方案结构非法 / 源格式不支持 / 备份失败等致命错误
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import shutil
import sys
import tempfile
import wave
from typing import Any, Dict, List, Tuple

# 每个片段必须包含的五个字段及其允许类型（bool 不是 int，需另行排除）
REQUIRED_FIELDS: Dict[str, Tuple[type, ...]] = {
    "start": (int, float),
    "end": (int, float),
    "fade_in": (int, float),
    "fade_out": (int, float),
    "gain_db": (int, float),
}

FATAL_EXIT_CODE = 2


class CliError(Exception):
    """致命错误：打印到 stderr 并以非零码退出，且不产生输出文件。"""


def _is_real_number(value: Any) -> bool:
    """字段类型检查：接受 int/float，但拒绝 bool、NaN、Infinity。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def load_plan(plan_path: str) -> List[Dict[str, Any]]:
    """读取并校验方案 JSON 的顶层结构，返回片段列表。

    片段内部字段由 :func:`validate_segment` 逐条校验，以便报错带上片段序号。
    """
    try:
        with open(plan_path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except FileNotFoundError:
        raise CliError(f"方案文件不存在：{plan_path}")
    except IsADirectoryError:
        raise CliError(f"方案路径是目录而非文件：{plan_path}")
    except PermissionError:
        raise CliError(f"方案文件不可读取（权限不足）：{plan_path}")
    except json.JSONDecodeError as exc:
        raise CliError(f"方案 JSON 解析失败：{plan_path}（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）")
    except OSError as exc:
        raise CliError(f"方案文件无法读取：{plan_path}（{exc}）")

    if not isinstance(plan, dict) or "segments" not in plan:
        raise CliError("方案 JSON 顶层必须是对象，且包含 'segments' 片段数组字段")
    segments = plan["segments"]
    if not isinstance(segments, list):
        raise CliError("方案字段 'segments' 必须是数组")
    return segments


def validate_segment(segment: Any, index: int) -> None:
    """校验单个片段的五个必填字段；任何问题都抛出带片段序号的 CliError。"""
    if not isinstance(segment, dict):
        raise CliError(f"片段 {index} 必须是对象，实际类型为 {type(segment).__name__}")
    for field in REQUIRED_FIELDS:
        if field not in segment:
            raise CliError(f"片段 {index} 缺少必填字段 '{field}'")
    for field, allowed_types in REQUIRED_FIELDS.items():
        value = segment[field]
        if not _is_real_number(value):
            actual = type(value).__name__
            raise CliError(
                f"片段 {index} 字段 '{field}' 类型错误：期望数字（int/float），实际为 {actual}"
            )


def read_source_wav(source_path: str) -> Tuple[wave._wave_params, array.ArrayType]:
    """读取源 WAV，要求为 16-bit PCM，返回参数与解交错后的采样数组。"""
    try:
        with wave.open(source_path, "rb") as wf:
            params = wf.getparams()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except FileNotFoundError:
        raise CliError(f"源 WAV 文件不存在：{source_path}")
    except IsADirectoryError:
        raise CliError(f"源 WAV 路径是目录而非文件：{source_path}")
    except PermissionError:
        raise CliError(f"源 WAV 文件不可读取（权限不足）：{source_path}")
    except wave.Error as exc:
        raise CliError(f"源 WAV 格式无法解析：{source_path}（{exc}）")
    except OSError as exc:
        raise CliError(f"源 WAV 文件无法读取：{source_path}（{exc}）")

    if params.sampwidth != 2:
        raise CliError(
            f"不支持的采样宽度：源文件为 {params.sampwidth * 8}-bit，"
            "本工具仅支持 16-bit PCM WAV"
        )
    # wave 模块只能打开 PCM/压缩格式头；非 PCM 压缩格式在此被显式拦截。
    if params.comptype != "NONE":
        raise CliError(f"不支持的压缩格式：{params.comptype}，本工具仅支持 16-bit PCM WAV")
    if params.nchannels < 1:
        raise CliError("源 WAV 通道数非法")

    samples = array.array("h")
    samples.frombytes(raw)
    return params, samples


def validate_segment_ranges(
    segment: Dict[str, float], index: int, total_frames: int, sample_rate: int
) -> Tuple[int, int, float, float, float]:
    """校验片段时间范围与淡入淡出时长，返回换算后的帧参数。

    任何非法情况抛出 :class:`SkippedSegment`，由调用方记入报告并跳过。
    """
    start = float(segment["start"])
    end = float(segment["end"])
    fade_in = float(segment["fade_in"])
    fade_out = float(segment["fade_out"])
    gain_db = float(segment["gain_db"])

    if start < 0 or end < 0 or fade_in < 0 or fade_out < 0:
        raise SkippedSegment("时间/淡入淡出参数不允许为负数")
    if end <= start:
        raise SkippedSegment(f"片段时长为零或为负（start={start}s, end={end}s）")
    if start >= total_frames / sample_rate:
        raise SkippedSegment(
            f"起点越界：start={start}s 超出源音频时长 {total_frames / sample_rate:.6f}s"
        )

    # 四舍五入换算到帧，保证确定性；结束点超出源长度则截断到末帧。
    start_frame = int(round(start * sample_rate))
    end_frame = int(round(end * sample_rate))
    start_frame = max(0, min(start_frame, total_frames))
    end_frame = max(0, min(end_frame, total_frames))
    if end_frame <= start_frame:
        raise SkippedSegment("裁剪后片段帧数为零（起止时间落在同一采样点或完全越界）")

    segment_frames = end_frame - start_frame
    fade_in_frames = int(round(fade_in * sample_rate))
    fade_out_frames = int(round(fade_out * sample_rate))
    if fade_in_frames > segment_frames:
        # 淡入超出片段长度时按比例收缩，而非直接判失败
        fade_in_frames = segment_frames
    if fade_out_frames > segment_frames:
        fade_out_frames = segment_frames
    if fade_in_frames + fade_out_frames > segment_frames:
        # 两者重叠时按比例缩短，保证淡入段与淡出段互不重叠
        overlap = fade_in_frames + fade_out_frames - segment_frames
        fade_in_frames -= overlap // 2
        fade_out_frames = segment_frames - fade_in_frames

    return start_frame, end_frame, float(fade_in_frames), float(fade_out_frames), gain_db


class SkippedSegment(Exception):
    """片段级跳过信号：记录原因，其他片段继续处理。"""


def render_segment(
    samples: array.ArrayType,
    channels: int,
    start_frame: int,
    end_frame: int,
    fade_in_frames: float,
    fade_out_frames: float,
    gain_db: float,
) -> array.ArrayType:
    """对单个片段应用线性淡入、淡出与增益，返回新的有符号 16-bit 采样数组。"""
    gain = 10.0 ** (gain_db / 20.0)
    segment_frames = end_frame - start_frame
    rendered = array.array("h", [0]) * (segment_frames * channels)

    for frame_index in range(segment_frames):
        if frame_index < fade_in_frames:
            fade = (frame_index + 1) / fade_in_frames  # 线性淡入：首帧 1/N，末帧 1.0
        elif frame_index >= segment_frames - fade_out_frames:
            # 线性淡出：进入淡出段时为 1.0，末帧为 1/N（不产生突变爆音）
            fade = (segment_frames - frame_index) / fade_out_frames
        else:
            fade = 1.0
        factor = gain * fade
        source_base = (start_frame + frame_index) * channels
        target_base = frame_index * channels
        for ch in range(channels):
            value = samples[source_base + ch] * factor
            # 增益后限幅，防止 16-bit 整数溢出
            if value > 32767.0:
                value = 32767.0
            elif value < -32768.0:
                value = -32768.0
            rendered[target_base + ch] = int(value)
    return rendered


def backup_existing_output(output_path: str) -> None:
    """输出路径已存在时先备份为 <output>.bak；备份失败则抛错中止，不写新输出。"""
    if not os.path.exists(output_path):
        return
    backup_path = output_path + ".bak"
    try:
        shutil.copy2(output_path, backup_path)
    except OSError as exc:
        raise CliError(
            f"输出文件备份失败，已中止且未写入新输出：{backup_path}（{exc}）"
        )


def write_output_wav(
    output_path: str, params: wave._wave_params, pcm_bytes: bytes
) -> None:
    """以与源一致的通道数/采样宽度/采样率写出 WAV，采用临时文件 + 原子替换。"""
    directory = os.path.dirname(os.path.abspath(output_path)) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp", prefix=".wav_editor_", dir=directory)
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            with wave.open(fh, "wb") as wf:
                wf.setnchannels(params.nchannels)
                wf.setsampwidth(params.sampwidth)
                wf.setframerate(params.framerate)
                wf.setcomptype("NONE", "not compressed")
                wf.writeframes(pcm_bytes)
        os.replace(tmp_path, output_path)
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise CliError(f"输出 WAV 写入失败：{output_path}（{exc}）")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wav_editor",
        description="读取 16-bit PCM WAV 与 JSON 剪辑方案，裁剪拼接并应用淡入淡出与增益。",
    )
    parser.add_argument("source_wav", help="源 WAV 文件路径（16-bit PCM）")
    parser.add_argument("plan_json", help="剪辑方案 JSON 路径（含 segments 数组）")
    parser.add_argument("output_wav", help="输出 WAV 文件路径")
    return parser


def run(source_path: str, plan_path: str, output_path: str) -> Dict[str, Any]:
    """核心流程，返回可 JSON 序列化的报告字典。"""
    if os.path.abspath(source_path) == os.path.abspath(output_path):
        raise CliError("输出路径不能与源 WAV 路径相同，拒绝原地覆盖源文件")

    segments = load_plan(plan_path)
    params, samples = read_source_wav(source_path)
    total_frames = params.nframes
    channels = params.nchannels

    output_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(output_dir):
        raise CliError(f"输出目录不存在或不是目录：{output_dir}")

    # 先完成全部片段的字段校验：结构非法属于致命错误，不生成输出文件。
    for index, segment in enumerate(segments):
        validate_segment(segment, index)

    # 备份必须在任何写出动作之前完成；失败则中止，原输出保持不变。
    backup_existing_output(output_path)

    report_segments: List[Dict[str, Any]] = []
    output_parts: List[bytes] = []
    output_frames = 0

    for index, segment in enumerate(segments):
        entry: Dict[str, Any] = {"index": index}
        try:
            start_frame, end_frame, fade_in_frames, fade_out_frames, gain_db = (
                validate_segment_ranges(segment, index, total_frames, params.framerate)
            )
            rendered = render_segment(
                samples,
                channels,
                start_frame,
                end_frame,
                fade_in_frames,
                fade_out_frames,
                gain_db,
            )
        except SkippedSegment as exc:
            entry.update(status="skipped", reason=str(exc))
            report_segments.append(entry)
            continue
        except Exception as exc:  # 防御性隔离：单片段任何意外都不拖垮整体
            entry.update(status="skipped", reason=f"处理异常：{type(exc).__name__}: {exc}")
            report_segments.append(entry)
            continue

        output_parts.append(rendered.tobytes())
        output_frames += end_frame - start_frame
        entry.update(
            status="ok",
            start_frame=start_frame,
            end_frame=end_frame,
            frames=end_frame - start_frame,
            gain_db=gain_db,
        )
        report_segments.append(entry)

    write_output_wav(output_path, params, b"".join(output_parts))

    ok_count = sum(1 for item in report_segments if item["status"] == "ok")
    skipped_count = len(report_segments) - ok_count
    report: Dict[str, Any] = {
        "source": os.path.abspath(source_path),
        "output": os.path.abspath(output_path),
        "sample_rate": params.framerate,
        "channels": params.nchannels,
        "sample_width_bytes": params.sampwidth,
        "source_total_frames": total_frames,
        "output_total_frames": output_frames,
        "output_total_samples": output_frames * channels,
        "segments_total": len(report_segments),
        "segments_ok": ok_count,
        "segments_skipped": skipped_count,
        "all_skipped": ok_count == 0,
        "segments": report_segments,
    }
    return report


def main(argv: List[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        report = run(args.source_wav, args.plan_json, args.output_wav)
    except CliError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return FATAL_EXIT_CODE
    except Exception as exc:  # 兜底：任何意外都给出明确错误而非裸 traceback
        print(
            f"错误：处理过程中发生未预期异常 {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return FATAL_EXIT_CODE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
