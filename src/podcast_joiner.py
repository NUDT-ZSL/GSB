# -*- coding: utf-8 -*-
"""
播客片段拼接与响度统一工具 (podcast_joiner)

功能：
  1. 读取描述文件 JSON（采样率、基准响度、片段列表），按顺序拼接各片段有效区间。
  2. 计算每个片段的 RMS 响度（dBFS），与基准响度比较后施加线性增益，并做防削波限幅。
  3. 对缺失文件、无效区间、采样率不匹配等失败片段进行隔离，成功片段照常产出。
  4. 全部片段失败时仍输出可被 wave 模块打开的空 WAV（0 数据帧）。
  5. 输出拼接 WAV（16-bit PCM 单声道）与处理报告 JSON。

仅依赖标准库（wave / audioop，audioop 在 Python 3.13+ 不可用时自动回退纯 Python 实现）。
"""

import argparse
import json
import math
import os
import struct
import sys
import wave

# ----------------------------------------------------------------------------
# 16-bit PCM 基础操作：优先使用 audioop，缺失时（Python 3.13+）使用纯 Python 回退
# ----------------------------------------------------------------------------
try:
    import audioop  # Python <= 3.12 标准库

    def pcm_rms(data: bytes) -> int:
        """返回片段 RMS（线性幅度，0..32767 量级）。"""
        return audioop.rms(data, 2)

    def pcm_minmax(data: bytes):
        """返回 (最小样本, 最大样本)。"""
        if not data:
            return 0, 0
        return audioop.minmax(data, 2)

    def pcm_mul(data: bytes, factor: float) -> bytes:
        """按线性系数缩放样本，audioop 内部自动削波。"""
        if factor == 1.0 or not data:
            return data
        return audioop.mul(data, 2, factor)

except ImportError:  # Python 3.13 移除了 audioop
    import array as _array

    def pcm_rms(data: bytes) -> int:
        n = len(data) // 2
        if n == 0:
            return 0
        total = 0
        for (s,) in struct.iter_unpack("<h", data):
            total += s * s
        return int(round(math.sqrt(total / n)))

    def pcm_minmax(data: bytes):
        if not data:
            return 0, 0
        lo, hi = 32768, -32769
        for (s,) in struct.iter_unpack("<h", data):
            if s < lo:
                lo = s
            if s > hi:
                hi = s
        return lo, hi

    def pcm_mul(data: bytes, factor: float) -> bytes:
        if factor == 1.0 or not data:
            return data
        samples = _array.array("h")
        samples.frombytes(data)
        scaled = _array.array(
            "h",
            (max(-32768, min(32767, int(s * factor))) for s in samples),
        )
        return scaled.tobytes()


# 报告中允许使用的片段状态值
STATUS_SUCCESS = "成功"
STATUS_MISSING = "缺失"
STATUS_INVALID_RANGE = "无效区间"
STATUS_RATE_MISMATCH = "采样率不匹配"
ALLOWED_STATUSES = {
    STATUS_SUCCESS,
    STATUS_MISSING,
    STATUS_INVALID_RANGE,
    STATUS_RATE_MISMATCH,
}

# 业务约束
MIN_REF_DB = -60.0
MAX_REF_DB = 0.0
MAX_TOTAL_SECONDS = 10 * 60.0      # 总时长上限 10 分钟
LONG_SEGMENT_SECONDS = 5 * 60.0    # 单片段超过 5 分钟给出警告
EPS = 1e-6


class DescriptionError(Exception):
    """描述文件本身的问题（格式、字段、基准响度非法等）。"""


def rms_to_dbfs(rms: int):
    """线性 RMS 转 dBFS；静音（rms<=0）返回 None。"""
    if rms <= 0:
        return None
    return 20.0 * math.log10(rms / 32768.0)


def load_description(desc_path: str) -> dict:
    """读取并校验描述文件，返回解析后的 dict。"""
    if not os.path.isfile(desc_path):
        raise DescriptionError(f"描述文件不存在或不可读取: {desc_path}")
    try:
        with open(desc_path, "r", encoding="utf-8") as f:
            desc = json.load(f)
    except json.JSONDecodeError as exc:
        raise DescriptionError(f"描述文件不是合法 JSON: {exc}") from exc
    except OSError as exc:
        raise DescriptionError(f"描述文件读取失败: {exc}") from exc

    if not isinstance(desc, dict):
        raise DescriptionError("描述文件根节点必须是 JSON 对象")

    missing_keys = [
        k for k in ("output_file", "sample_rate", "target_loudness_db", "segments")
        if k not in desc
    ]
    if missing_keys:
        raise DescriptionError(f"描述文件缺少必填字段: {', '.join(missing_keys)}")

    if not isinstance(desc["segments"], list) or not desc["segments"]:
        raise DescriptionError("segments 必须是非空列表")

    sample_rate = desc["sample_rate"]
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise DescriptionError("sample_rate 必须是正整数")

    ref = desc["target_loudness_db"]
    if isinstance(ref, bool) or not isinstance(ref, (int, float)):
        raise DescriptionError("target_loudness_db 必须是数值")
    # 基准响度范围校验：超范围直接报错，不产生任何输出
    if not (MIN_REF_DB <= float(ref) <= MAX_REF_DB):
        raise DescriptionError(
            f"基准响度无效：允许范围为 {MIN_REF_DB:g} 至 {MAX_REF_DB:g} dB，"
            f"当前值为 {float(ref):g} dB"
        )

    out_name = str(desc["output_file"]).strip()
    if not out_name:
        raise DescriptionError("output_file 不能为空")

    for i, seg in enumerate(desc["segments"]):
        if not isinstance(seg, dict):
            raise DescriptionError(f"第 {i + 1} 个片段必须是对象")
        for k in ("path", "start_sec", "end_sec"):
            if k not in seg:
                raise DescriptionError(f"第 {i + 1} 个片段缺少字段 {k}")

    return desc


def process(desc_path: str, output_dir: str) -> dict:
    """
    主处理流程。返回报告 dict；WAV 与报告 JSON 写入 output_dir。
    片段级失败被隔离并记录，不抛出异常；描述文件级问题抛 DescriptionError。
    """
    desc = load_description(desc_path)
    desc_dir = os.path.dirname(os.path.abspath(desc_path))

    target_sr = int(desc["sample_rate"])
    target_db = float(desc["target_loudness_db"])
    out_name = os.path.basename(str(desc["output_file"]).strip())
    stem, _ = os.path.splitext(out_name)
    report_name = stem + "_report.json"

    os.makedirs(output_dir, exist_ok=True)
    out_wav_path = os.path.join(output_dir, out_name)
    out_report_path = os.path.join(output_dir, report_name)
    overwritten = os.path.exists(out_wav_path)

    entries = []
    output_chunks = []
    total_frames = 0

    for idx, seg in enumerate(desc["segments"], start=1):
        rel_path = str(seg["path"])
        seg_path = rel_path if os.path.isabs(rel_path) else os.path.join(desc_dir, rel_path)
        start = float(seg["start_sec"])
        end = float(seg["end_sec"])

        entry = {
            "index": idx,
            "path": rel_path,
            "start_sec": start,
            "end_sec": end,
            "status": None,
            "reason": None,
            "original_rms_db": None,
            "applied_gain_db": None,
            "effective_duration_sec": None,
            "warning": None,
        }

        # ---- 1) 文件可读取性（缺失/无法读取 → 隔离） ----
        try:
            wf = wave.open(seg_path, "rb")
        except (OSError, wave.Error, EOFError) as exc:
            entry["status"] = STATUS_MISSING
            entry["reason"] = f"片段文件不存在或无法读取: {exc.__class__.__name__}: {exc}"
            entries.append(entry)
            continue

        with wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            duration = nframes / framerate if framerate else 0.0

            # 仅支持 16-bit PCM 单声道，其余按无法读取隔离
            if nchannels != 1 or sampwidth != 2:
                entry["status"] = STATUS_MISSING
                entry["reason"] = (
                    f"片段格式不支持（仅支持16位PCM单声道）: "
                    f"channels={nchannels}, sampwidth={sampwidth}"
                )
                entries.append(entry)
                continue

            # ---- 2) 采样率匹配 ----
            if framerate != target_sr:
                entry["status"] = STATUS_RATE_MISMATCH
                entry["reason"] = (
                    f"采样率不匹配：片段实际 {framerate} Hz，描述文件指定 {target_sr} Hz"
                )
                entries.append(entry)
                continue

            # ---- 3) 区间有效性 ----
            if (
                start < 0
                or end < 0
                or end <= start
                or start > duration + EPS
                or end > duration + EPS
            ):
                entry["status"] = STATUS_INVALID_RANGE
                if start < 0 or end < 0:
                    reason = "起止秒数不能为负"
                elif end <= start:
                    reason = "结束秒数必须晚于开始秒数"
                else:
                    reason = f"区间超出文件时长（文件时长 {duration:.3f}s）"
                entry["reason"] = reason
                entries.append(entry)
                continue

            # ---- 4) 读取有效区间 ----
            start_frame = max(0, int(round(start * framerate)))
            end_frame = min(nframes, int(round(end * framerate)))
            if end_frame <= start_frame:
                entry["status"] = STATUS_INVALID_RANGE
                entry["reason"] = "有效区间换算后不含任何采样帧"
                entries.append(entry)
                continue

            wf.setpos(start_frame)
            fragment = wf.readframes(end_frame - start_frame)

        frag_frames = len(fragment) // 2
        effective_duration = frag_frames / framerate

        # ---- 5) RMS 响度与线性增益（防削波） ----
        rms = pcm_rms(fragment)
        rms_db = rms_to_dbfs(rms)
        if rms_db is None:
            gain_db = 0.0
            factor = 1.0
        else:
            gain_db = target_db - rms_db
            factor = 10.0 ** (gain_db / 20.0)
            lo, hi = pcm_minmax(fragment)
            peak = max(abs(lo), abs(hi))
            if peak > 0:
                # 保证 peak * factor <= 32767，调整后样本绝不溢出
                safe_factor = 32767.0 / peak
                if factor > safe_factor:
                    factor = safe_factor
                    gain_db = 20.0 * math.log10(factor)

        adjusted = pcm_mul(fragment, factor)

        # 防削波复核（理论上不会触发，触发即说明实现有误）
        adj_lo, adj_hi = pcm_minmax(adjusted)
        if adj_lo < -32768 or adj_hi > 32767:
            entry["status"] = STATUS_MISSING
            entry["reason"] = "内部错误：增益调整后样本溢出"
            entries.append(entry)
            continue

        entry["status"] = STATUS_SUCCESS
        entry["reason"] = None
        entry["original_rms_db"] = None if rms_db is None else round(rms_db, 3)
        entry["applied_gain_db"] = round(
            0.0 if rms_db is None else 20.0 * math.log10(factor), 3
        )
        entry["effective_duration_sec"] = round(effective_duration, 6)
        if effective_duration > LONG_SEGMENT_SECONDS:
            entry["warning"] = (
                f"单片段时长 {effective_duration:.1f}s 超过 {LONG_SEGMENT_SECONDS}s，"
                "已正常参与拼接"
            )

        output_chunks.append(adjusted)
        total_frames += frag_frames
        entries.append(entry)

    total_duration = total_frames / target_sr if target_sr else 0.0
    if total_duration > MAX_TOTAL_SECONDS + EPS:
        raise DescriptionError(
            f"拼接总时长 {total_duration:.1f}s 超过上限 {MAX_TOTAL_SECONDS:.0f}s"
        )

    # ---- 6) 写 WAV（全部失败时写 0 数据帧的空音频，仍可被 wave 打开） ----
    with wave.open(out_wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(target_sr)
        wf.writeframes(b"".join(output_chunks))

    succeeded = sum(1 for e in entries if e["status"] == STATUS_SUCCESS)
    report = {
        "description_file": os.path.abspath(desc_path),
        "output_wav": out_name,
        "sample_rate": target_sr,
        "target_loudness_db": target_db,
        "overwritten": overwritten,
        "segments": entries,
        "summary": {
            "total_segments": len(entries),
            "succeeded": succeeded,
            "failed": len(entries) - succeeded,
            "missing": sum(1 for e in entries if e["status"] == STATUS_MISSING),
            "invalid_range": sum(
                1 for e in entries if e["status"] == STATUS_INVALID_RANGE
            ),
            "sample_rate_mismatch": sum(
                1 for e in entries if e["status"] == STATUS_RATE_MISMATCH
            ),
            "warnings": sum(1 for e in entries if e["warning"]),
            "total_output_frames": total_frames,
            "total_output_duration_sec": round(total_duration, 6),
            "overwritten": overwritten,
        },
    }
    with open(out_report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return report


def main(argv=None) -> int:
    # Windows 重定向时避免中文编码异常
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="播客片段拼接与响度统一工具（16-bit PCM 单声道 WAV）"
    )
    parser.add_argument("description", help="描述文件 JSON 路径")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="可选输出目录；省略时输出到当前工作目录",
    )
    args = parser.parse_args(argv)

    output_dir = os.path.abspath(args.output_dir) if args.output_dir else os.getcwd()

    try:
        report = process(args.description, output_dir)
    except DescriptionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    s = report["summary"]
    print(
        f"处理完成：成功 {s['succeeded']}/{s['total_segments']} 个片段，"
        f"输出时长 {s['total_output_duration_sec']:.3f}s，"
        f"覆盖已有文件：{'是' if s['overwritten'] else '否'}"
    )
    print(f"WAV  : {os.path.join(output_dir, report['output_wav'])}")
    stem, _ = os.path.splitext(report["output_wav"])
    print(f"报告 : {os.path.join(output_dir, stem + '_report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
