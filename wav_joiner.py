# -*- coding: utf-8 -*-
"""
wav_joiner.py — 按 JSON 清单顺序拼接 PCM WAV 角色语音的命令行工具。

约束：
  * 仅使用 Python 标准库，要求 Python 3.10+
  * 仅处理 PCM 整数 WAV（8/16/24/32 位）与 32 位 IEEE 浮点 WAV
  * 成功拼接的片段采样率 / 声道数 / 位深必须与首个成功片段完全一致
  * 32 位浮点片段写出时转换为 32 位整数 PCM 并限幅，报告中注明转换
  * 头部声明长度与实际字节数不符等损坏文件、缺失路径、扩展名不符、
    无法解析的条目一律跳过并在报告中逐条说明，不影响其他片段
  * 全部片段不可用时不生成结果 WAV，但仍生成报告

退出码：
  0  正常结束（即使部分或全部片段被跳过）
  2  清单本身不可用（JSON 无法解析 / 缺少 segments / 条目非法），不产出任何音频
  3  结果文件写入失败
"""
from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
import wave
from dataclasses import dataclass
from datetime import datetime, timezone

# Windows 控制台默认可能是 GBK 代码页，统一强制 UTF-8，保证中文报告/错误可读
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------- #
# RIFF / WAVE 常量
# --------------------------------------------------------------------------- #
RIFF_ID = b"RIFF"
WAVE_ID = b"WAVE"
FMT_ID = b"fmt "
DATA_ID = b"data"

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003

SUPPORTED_PCM_BITS = (8, 16, 24, 32)

DEFAULT_OUTPUT_NAME = "joined.wav"
DEFAULT_REPORT_NAME = "report.txt"

INT32_MAX = 2_147_483_647
INT32_MIN = -2_147_483_648


class WavError(Exception):
    """WAV 文件无法解析或已损坏。"""


class ManifestError(Exception):
    """清单 JSON 无法解析或结构非法。"""


@dataclass(frozen=True)
class WaveInfo:
    """通过头部解析得到的 WAV 元数据（data 块不整体读入内存）。"""

    path: str
    format_tag: int
    channels: int
    sample_rate: int
    bits_per_sample: int
    block_align: int
    data_offset: int
    data_length: int
    frames: int
    is_float: bool


@dataclass
class SegmentRecord:
    """清单中一个条目的处理结论，顺序与清单一致。"""

    index: int
    raw_path: str
    status: str  # "成功" 或 "跳过"
    detail: str
    frames: int = 0
    converted: bool = False


# --------------------------------------------------------------------------- #
# WAV 头部解析（自研 RIFF 遍历，支持标准库 wave 不支持的 24bit / 浮点格式）
# --------------------------------------------------------------------------- #
def inspect_wav(path: str) -> WaveInfo:
    """解析 WAV 头部并严格校验声明长度与实际字节数。

    任何结构问题都抛 WavError，消息中带具体原因，供报告直接使用。
    """
    try:
        file_size = os.path.getsize(path)
    except OSError as exc:
        raise WavError(f"文件无法读取: {exc}") from exc

    with open(path, "rb") as f:
        header = f.read(12)
        if len(header) < 12 or header[0:4] != RIFF_ID or header[8:12] != WAVE_ID:
            raise WavError("不是有效的 RIFF/WAVE 文件，无法解析为 WAV")

        riff_size = struct.unpack("<I", header[4:8])[0]
        if riff_size + 8 != file_size:
            raise WavError(
                f"RIFF 声明总长度 {riff_size + 8} 字节与实际文件大小 "
                f"{file_size} 字节不符，文件损坏"
            )

        fmt_body: bytes | None = None
        data_chunk: tuple[int, int] | None = None
        offset = 12
        while offset + 8 <= file_size:
            chunk_header = f.read(8)
            if len(chunk_header) < 8:
                raise WavError("文件在子块头部处被截断，文件损坏")
            chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
            body_start = offset + 8
            body_end = body_start + chunk_size
            if body_end > file_size:
                name = chunk_id.decode("ascii", "replace")
                raise WavError(
                    f"{name} 块声明长度 {chunk_size} 字节，但实际数据不足"
                    f"（需要到第 {body_end} 字节，文件仅 {file_size} 字节），文件损坏"
                )
            if chunk_id == FMT_ID:
                f.seek(body_start)
                fmt_body = f.read(chunk_size)
            elif chunk_id == DATA_ID:
                data_chunk = (body_start, chunk_size)
            # RIFF 子块按字（2 字节）对齐，奇数长度带 1 个填充字节
            offset = body_end + (chunk_size & 1)
            f.seek(offset)

        if fmt_body is None:
            raise WavError("缺少 fmt 块，无法解析为 WAV")
        if data_chunk is None:
            raise WavError("缺少 data 块，无法解析为 WAV")
        if len(fmt_body) < 16:
            raise WavError(f"fmt 块过短（{len(fmt_body)} < 16 字节），文件损坏")

        (
            format_tag,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
        ) = struct.unpack("<HHIIHH", fmt_body[:16])

        if channels == 0:
            raise WavError("声道数为 0，参数非法")
        if sample_rate == 0:
            raise WavError("采样率为 0，参数非法")
        if block_align == 0:
            raise WavError("块对齐大小为 0，参数非法")

        if format_tag == WAVE_FORMAT_PCM:
            if bits_per_sample not in SUPPORTED_PCM_BITS:
                raise WavError(
                    f"不支持的 PCM 位深 {bits_per_sample} 位"
                    f"（仅支持 8/16/24/32 位整数）"
                )
        elif format_tag == WAVE_FORMAT_IEEE_FLOAT:
            if bits_per_sample != 32:
                raise WavError(
                    f"浮点 WAV 仅支持 32 位，收到 {bits_per_sample} 位浮点"
                )
        else:
            raise WavError(
                f"非 PCM 编码（format_tag=0x{format_tag:04x}），"
                f"仅支持 PCM 整数与 32 位 IEEE 浮点"
            )

        expected_block_align = channels * bits_per_sample // 8
        if block_align != expected_block_align:
            raise WavError(
                f"块对齐 {block_align} 与 声道数×位深/8="
                f"{expected_block_align} 不一致，文件损坏"
            )
        if byte_rate != sample_rate * block_align:
            raise WavError(
                f"平均字节率 {byte_rate} 与 采样率×块对齐="
                f"{sample_rate * block_align} 不一致，文件损坏"
            )

        data_offset, data_length = data_chunk
        if data_length % block_align != 0:
            raise WavError(
                f"data 长度 {data_length} 字节不是帧大小 {block_align} "
                f"字节的整数倍，文件损坏"
            )

        return WaveInfo(
            path=path,
            format_tag=format_tag,
            channels=channels,
            sample_rate=sample_rate,
            bits_per_sample=bits_per_sample,
            block_align=block_align,
            data_offset=data_offset,
            data_length=data_length,
            frames=data_length // block_align,
            is_float=format_tag == WAVE_FORMAT_IEEE_FLOAT,
        )


def read_frame_data(info: WaveInfo) -> bytes:
    """按头部声明精确读取 data 块字节；读取不足即损坏。"""
    with open(info.path, "rb") as f:
        f.seek(info.data_offset)
        data = f.read(info.data_length)
    if len(data) != info.data_length:
        raise WavError(
            f"data 实际可读 {len(data)} 字节，少于声明的 "
            f"{info.data_length} 字节，文件损坏"
        )
    return data


# --------------------------------------------------------------------------- #
# 32 位浮点 -> 32 位整数 PCM（限幅，避免溢出）
# --------------------------------------------------------------------------- #
def float32_bytes_to_int32(data: bytes) -> bytes:
    """把 32 位 IEEE 浮点采样逐样本转为 32 位有符号整数。

    约定浮点范围 [-1.0, 1.0]：
      * |x| >= 1.0        -> 限幅到 INT32_MAX / INT32_MIN
      * NaN              -> 0
      * 其余按幅度线性缩放，正负对称四舍五入（远离零方向）
    """
    count = len(data) // 4
    out = bytearray(len(data))
    for i in range(count):
        (x,) = struct.unpack_from("<f", data, i * 4)
        if math.isnan(x):
            n = 0
        elif x >= 1.0:
            n = INT32_MAX
        elif x <= -1.0:
            n = INT32_MIN
        else:
            magnitude = math.floor(abs(x) * INT32_MAX + 0.5)
            n = magnitude if x >= 0.0 else -magnitude
        struct.pack_into("<i", out, i * 4, n)
    return bytes(out)


# --------------------------------------------------------------------------- #
# 清单加载
# --------------------------------------------------------------------------- #
def load_manifest(manifest_path: str) -> list[str]:
    """加载并校验清单，返回片段原始路径列表（保持清单顺序）。"""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError as exc:
        raise ManifestError(f"清单文件不存在: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"清单 JSON 无法解析: {exc.msg}（第 {exc.lineno} 行，"
            f"第 {exc.colno} 列）"
        ) from exc
    except OSError as exc:
        raise ManifestError(f"清单文件无法读取: {exc}") from exc

    if not isinstance(doc, dict) or "segments" not in doc:
        raise ManifestError("清单缺少片段列表（顶层必须是含 segments 数组的对象）")

    raw_segments = doc["segments"]
    if not isinstance(raw_segments, list):
        raise ManifestError("清单 segments 字段必须是数组")

    entries: list[str] = []
    for idx, item in enumerate(raw_segments):
        if isinstance(item, str):
            entries.append(item)
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            entries.append(item["path"])
        else:
            raise ManifestError(
                f"第 {idx + 1} 个片段条目非法：应为路径字符串或含 path 字段的对象"
            )
    return entries


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def build_report(
    manifest_path: str,
    records: list[SegmentRecord],
    reference: WaveInfo | None,
    output_path: str | None,
    total_frames: int,
) -> str:
    lines: list[str] = []
    lines.append("WAV 角色语音拼接处理报告")
    lines.append("=" * 60)
    lines.append(f"清单文件: {manifest_path}")
    lines.append(f"生成时间(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    if reference is not None:
        kind = "32 位浮点(输出转整数 PCM)" if reference.is_float else "PCM 整数"
        lines.append(
            f"基准参数(首个成功片段): 采样率={reference.sample_rate}Hz, "
            f"声道数={reference.channels}, 位深={reference.bits_per_sample} 位, "
            f"编码={kind}"
        )
    else:
        lines.append("基准参数: 无（没有任何可用片段）")

    success = [r for r in records if r.status == "成功"]
    skipped = [r for r in records if r.status == "跳过"]
    lines.append(
        f"片段统计: 总计 {len(records)} 条, 成功 {len(success)} 条, "
        f"跳过 {len(skipped)} 条"
    )
    lines.append("")
    lines.append("逐条明细（行序与清单顺序完全一致，重复条目分别记录）:")
    lines.append("-" * 60)
    for rec in records:
        head = f"[{rec.index:02d}] {rec.status}"
        if rec.status == "成功":
            convert_flag = "，已做浮点->整数转换" if rec.converted else ""
            lines.append(
                f"{head} | 文件={rec.raw_path} | 帧数={rec.frames}"
                f"{convert_flag} | 说明={rec.detail}"
            )
        else:
            lines.append(f"{head} | 文件={rec.raw_path} | 原因={rec.detail}")
    lines.append("-" * 60)
    if output_path is not None:
        lines.append(
            f"输出结果: {output_path}（PCM 整数 WAV，总采样帧数={total_frames}）"
        )
    else:
        lines.append("输出结果: 无成功片段，未生成结果 WAV；请按上述原因逐一补录素材")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(
    manifest_path: str,
    output_dir: str,
    output_name: str = DEFAULT_OUTPUT_NAME,
    report_name: str = DEFAULT_REPORT_NAME,
) -> int:
    # 清单错误直接抛出，由 main 处理；此阶段不触碰输出目录，保证不产出音频
    entries = load_manifest(manifest_path)
    base_dir = os.path.dirname(os.path.abspath(manifest_path))

    records: list[SegmentRecord] = []
    accepted: list[tuple[WaveInfo, SegmentRecord]] = []
    reference: WaveInfo | None = None

    for idx, raw_path in enumerate(entries, start=1):
        abs_path = (
            raw_path
            if os.path.isabs(raw_path)
            else os.path.normpath(os.path.join(base_dir, raw_path))
        )
        rec = SegmentRecord(index=idx, raw_path=raw_path, status="跳过", detail="")

        if os.path.splitext(raw_path)[1].lower() != ".wav":
            rec.detail = "扩展名不是 .wav，按不可用条目跳过"
            records.append(rec)
            continue
        if not os.path.isfile(abs_path):
            rec.detail = "路径不存在或不是常规文件，按不可用条目跳过"
            records.append(rec)
            continue

        try:
            info = inspect_wav(abs_path)
        except WavError as exc:
            rec.detail = str(exc)
            records.append(rec)
            continue
        except OSError as exc:
            rec.detail = f"文件无法读取: {exc}"
            records.append(rec)
            continue

        # 以首个成功片段为基准；采样率 / 声道数 / 位深必须完全一致
        if reference is None:
            reference = info
        else:
            mismatches: list[str] = []
            if info.sample_rate != reference.sample_rate:
                mismatches.append(
                    f"采样率 {info.sample_rate}Hz != 基准 {reference.sample_rate}Hz"
                )
            if info.channels != reference.channels:
                mismatches.append(
                    f"声道数 {info.channels} != 基准 {reference.channels}"
                )
            if info.bits_per_sample != reference.bits_per_sample:
                mismatches.append(
                    f"位深 {info.bits_per_sample} 位 != 基准 "
                    f"{reference.bits_per_sample} 位"
                )
            if mismatches:
                rec.detail = (
                    "音频参数与首个成功片段不一致（"
                    + "；".join(mismatches)
                    + "），跳过且不写入结果"
                )
                records.append(rec)
                continue

        rec.status = "成功"
        rec.frames = info.frames
        rec.converted = info.is_float
        if info.is_float:
            rec.detail = "32 位浮点采样已限幅并转换为 32 位整数 PCM，纳入拼接"
        else:
            rec.detail = "参数与基准一致，纳入拼接"
        records.append(rec)
        accepted.append((info, rec))

    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, output_name)
    report_path = os.path.join(output_dir, report_name)
    total_frames = 0
    written_path: str | None = None

    if accepted:
        try:
            # 输出始终为整数 PCM，参数与首个成功片段一致
            with wave.open(output_path, "wb") as wf:
                wf.setnchannels(reference.channels)
                wf.setsampwidth(reference.bits_per_sample // 8)
                wf.setframerate(reference.sample_rate)
                for info, _rec in accepted:  # 严格按清单顺序连续写入，无静音/间隙
                    data = read_frame_data(info)
                    if info.is_float:
                        data = float32_bytes_to_int32(data)
                    wf.writeframes(data)
                    total_frames += info.frames
            written_path = output_path
        except (OSError, WavError, wave.Error) as exc:
            try:
                os.remove(output_path)
            except OSError:
                pass
            print(f"错误: 结果 WAV 写入失败: {exc}", file=sys.stderr)
            return 3
    else:
        # 全部不可用：确保目录里不存在本次运行产生/遗留的结果 WAV
        if os.path.exists(output_path):
            os.remove(output_path)

    report_text = build_report(
        manifest_path, records, reference, written_path, total_frames
    )
    with open(report_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(report_text)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按 JSON 清单顺序把 PCM WAV 角色语音拼接为统一参数的完整音轨，"
        "并生成逐片段文本报告。"
    )
    parser.add_argument("manifest", help="JSON 清单路径（顶层含 segments 数组）")
    parser.add_argument("output_dir", help="输出目录（生成 joined.wav 与 report.txt）")
    parser.add_argument(
        "--output-name", default=DEFAULT_OUTPUT_NAME, help="结果 WAV 文件名（默认 joined.wav）"
    )
    parser.add_argument(
        "--report-name", default=DEFAULT_REPORT_NAME, help="报告文件名（默认 report.txt）"
    )
    args = parser.parse_args(argv)

    try:
        return run(args.manifest, args.output_dir, args.output_name, args.report_name)
    except ManifestError as exc:
        # 清单非法：不创建任何音频文件，非 0 退出并给出可读错误
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
