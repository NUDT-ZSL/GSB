# -*- coding: utf-8 -*-
"""
对白片段批量处理流水线（仅使用 Python 标准库）。

功能：
  1. 读取编辑清单 JSON，校验每个片段是否存在且为 16 位单声道 PCM WAV；
  2. 按起止毫秒裁剪，区间越界截断到有效范围，非法区间/空结果记为跳过；
  3. 统一重采样到清单指定目标采样率（线性插值）；
  4. 按目标 dBFS 做 RMS 响度归一化（误差控制在量化误差范围内）；
  5. 按清单顺序拼接成功片段，片段间插入指定毫秒静音，跳过/错误片段不占时间；
  6. 输出逐条 JSON 报告与整体总时长；
  7. 运行日志写入文本文件（每行含时间戳、级别 INFO/ERROR、消息）；
  8. 命令行接受清单、输出音频、报告、日志四个路径，缺参时打印用法并以非 0 退出。

用法：
  python process.py --manifest edit_list.json --output out.wav \
                    --report report.json --log run.log
"""

import argparse
import array
import json
import logging
import math
import os
import sys
import wave


# --------------------------------------------------------------------------- #
# 命令行参数解析：缺参时把用法打印到标准输出并以非 0 退出码结束
# --------------------------------------------------------------------------- #
class StdoutArgumentParser(argparse.ArgumentParser):
    """argparse 默认把用法/错误输出到 stderr，本任务要求打印到标准输出。"""

    def print_help(self, file=None):
        super().print_help(sys.stdout)

    def format_usage(self):
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions,
                            self._mutually_exclusive_groups)
        return formatter.format_help()

    def exit(self, status=0, message=None):
        if message:
            sys.stdout.write(message)
        if status != 0:
            # 缺少必需参数等错误：先输出完整用法说明
            self.print_help()
        raise SystemExit(status)

    def error(self, message):
        sys.stdout.write("错误: %s\n\n" % message)
        self.exit(2)


# --------------------------------------------------------------------------- #
# WAV 读取与校验
# --------------------------------------------------------------------------- #
def read_pcm16_mono_wav(path):
    """读取 16 位单声道 PCM WAV。

    成功返回 (采样率, samples)，samples 为 array('h')。
    失败抛出 ValueError，消息为可直接写入报告的中文原因。
    """
    if not os.path.isfile(path):
        raise ValueError("文件不存在: %s" % path)
    try:
        with wave.open(path, "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except wave.Error as exc:
        # 例如: unknown format: 3（IEEE float 等非 PCM 格式）
        raise ValueError("非PCM或无法解析的WAV: %s" % exc)
    except (EOFError, OSError) as exc:
        raise ValueError("WAV文件损坏或无法读取: %s" % exc)

    if sampwidth != 2:
        raise ValueError("格式不符: 要求16位PCM, 实际采样宽度%d字节" % sampwidth)
    if n_channels != 1:
        raise ValueError("格式不符: 要求单声道, 实际%d声道" % n_channels)

    samples = array.array("h")
    samples.frombytes(raw)
    return sample_rate, samples


def write_pcm16_mono_wav(path, sample_rate, samples):
    """写出 16 位单声道 PCM WAV，头部反映实际采样率与声道数。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(samples.tobytes())


# --------------------------------------------------------------------------- #
# 裁剪（毫秒 -> 帧，越界截断，非法区间跳过）
# --------------------------------------------------------------------------- #
def crop_samples(samples, sample_rate, start_ms, end_ms):
    """按起止毫秒裁剪。

    返回 (cropped, reason)。reason 为 None 表示成功，否则为跳过原因。
    起止非法（非数值/负数/起始大于结束）或裁剪后为空 -> 跳过。
    区间越界自动截断到 [0, 总时长]。
    """
    for name, value in (("start_ms", start_ms), ("end_ms", end_ms)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "裁剪区间非法: %s=%r 不是数值" % (name, value)
        if value < 0:
            return None, "裁剪区间非法: %s=%r 为负数" % (name, value)
    if start_ms > end_ms:
        return None, "裁剪区间非法: 起始(%sms)大于结束(%sms)" % (start_ms, end_ms)

    total_frames = len(samples)
    duration_ms = total_frames * 1000.0 / sample_rate

    # 越界截断到有效范围
    start_ms_clamped = min(float(start_ms), duration_ms)
    end_ms_clamped = min(float(end_ms), duration_ms)
    if end_ms_clamped <= start_ms_clamped:
        return None, "裁剪后为空: 截断后区间[%.3f, %.3f]ms无有效采样" % (
            start_ms_clamped, end_ms_clamped)

    start_frame = int(round(start_ms_clamped * sample_rate / 1000.0))
    end_frame = int(round(end_ms_clamped * sample_rate / 1000.0))
    start_frame = max(0, min(start_frame, total_frames))
    end_frame = max(0, min(end_frame, total_frames))

    if end_frame <= start_frame:
        return None, "裁剪后为空: 起止落在同一采样帧(%d)" % start_frame

    return samples[start_frame:end_frame], None


# --------------------------------------------------------------------------- #
# 重采样：线性插值，仅依赖标准库
# --------------------------------------------------------------------------- #
def resample_linear(samples, src_rate, dst_rate):
    """将 16 位 PCM 采样序列从 src_rate 线性插值重采样到 dst_rate。"""
    if dst_rate == src_rate:
        return array.array("h", samples)

    n_src = len(samples)
    n_dst = int(round(n_src * dst_rate / src_rate))
    out = array.array("h")
    if n_src == 0:
        return out
    if n_src == 1:
        out.extend([samples[0]] * n_dst)
        return out

    ratio = src_rate / dst_rate
    prev = samples[0]
    for i in range(n_dst):
        src_pos = i * ratio
        idx = int(src_pos)
        frac = src_pos - idx
        if idx >= n_src - 1:
            value = samples[n_src - 1]
        else:
            cur = samples[idx]
            nxt = samples[idx + 1]
            value = cur + (nxt - cur) * frac
        iv = int(round(value))
        if iv > 32767:
            iv = 32767
        elif iv < -32768:
            iv = -32768
        out.append(iv)
    return out


# --------------------------------------------------------------------------- #
# 响度归一化：处理后 RMS 达到目标 dBFS
# --------------------------------------------------------------------------- #
def rms_dbfs(samples):
    """计算 RMS 并换算为 dBFS（满刻度 32768）。全静音返回负无穷。"""
    if not samples:
        return float("-inf")
    total = 0
    for s in samples:
        total += s * s
    rms = math.sqrt(total / len(samples))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms / 32768.0)


def normalize_loudness(samples, target_dbfs):
    """按目标 dBFS 做整体增益归一化，结果裁剪到 16 位范围。

    返回 (归一化后 array('h'), 原因)。全静音片段无法归一化，记为跳过。
    """
    target_linear = 32768.0 * (10.0 ** (target_dbfs / 20.0))
    total = 0
    for s in samples:
        total += s * s
    rms = math.sqrt(total / len(samples)) if samples else 0.0
    if rms <= 0.0:
        return None, "片段为全静音, 无法归一化到目标响度"

    gain = target_linear / rms
    out = array.array("h")
    for s in samples:
        v = int(round(s * gain))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(v)
    return out, None


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
class _MillisFormatter(logging.Formatter):
    """时间戳精确到毫秒，格式: YYYY-MM-DD HH:MM:SS.mmm LEVEL 消息"""

    def formatTime(self, record, datefmt=None):
        ct = self.converter(record.created)
        msec = record.msecs
        return "%04d-%02d-%02d %02d:%02d:%02d.%03d" % (
            ct.tm_year, ct.tm_mon, ct.tm_mday,
            ct.tm_hour, ct.tm_min, ct.tm_sec, int(msec))


def setup_logger(log_path):
    parent = os.path.dirname(os.path.abspath(log_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    logger = logging.getLogger("wav_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(_MillisFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser():
    parser = StdoutArgumentParser(
        prog="process.py",
        description="16位单声道PCM WAV对白片段裁剪/重采样/响度归一化/拼接工具",
    )
    parser.add_argument("--manifest", required=True, help="编辑清单 JSON 路径")
    parser.add_argument("--output", required=True, help="拼接输出 WAV 路径")
    parser.add_argument("--report", required=True, help="JSON 报告输出路径")
    parser.add_argument("--log", required=True, help="运行日志文本文件路径")
    return parser


def load_manifest(path, logger):
    if not os.path.isfile(path):
        logger.error("清单文件不存在: %s", path)
        raise SystemExit(2)
    try:
        # utf-8-sig 兼容 Windows 编辑器常见的 UTF-8 BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("清单解析失败: %s", exc)
        raise SystemExit(2)
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        logger.error("清单结构非法: 顶层需为对象且包含 segments 数组")
        raise SystemExit(2)
    if "target_sample_rate" in data:
        tsr = data["target_sample_rate"]
        if isinstance(tsr, bool) or not isinstance(tsr, int) or tsr <= 0:
            logger.error("清单 target_sample_rate 非法: %r", tsr)
            raise SystemExit(2)
    return data


def process_segment(entry, base_dir, default_rate, logger):
    """处理单条清单，返回 (报告条目 dict, 成功时的采样 array 或 None)。"""
    file_name = entry.get("file")
    label = file_name if isinstance(file_name, str) else "<未知文件>"
    report_item = {
        "file": file_name,
        "status": "错误",
        "reason": None,
        "duration_seconds": None,
    }

    # 字段完整性校验
    required = {
        "file": str,
        "start_ms": (int, float),
        "end_ms": (int, float),
        "target_dbfs": (int, float),
        "silence_after_ms": (int, float),
    }
    for key, typ in required.items():
        if key not in entry:
            reason = "清单条目缺少字段: %s" % key
            report_item["reason"] = reason
            logger.error("%s: %s", label, reason)
            return report_item, None
        if isinstance(entry[key], bool) or not isinstance(entry[key], typ):
            reason = "清单条目字段类型错误: %s=%r" % (key, entry[key])
            report_item["reason"] = reason
            logger.error("%s: %s", label, reason)
            return report_item, None
    if entry["silence_after_ms"] < 0 or entry["target_dbfs"] >= 0:
        reason = "清单条目数值非法: silence_after_ms=%r target_dbfs=%r" % (
            entry["silence_after_ms"], entry["target_dbfs"])
        report_item["reason"] = reason
        logger.error("%s: %s", label, reason)
        return report_item, None

    target_rate = entry.get("target_sample_rate", default_rate)
    if (target_rate is None or isinstance(target_rate, bool)
            or not isinstance(target_rate, int) or target_rate <= 0):
        reason = "目标采样率非法: %r" % (target_rate,)
        report_item["reason"] = reason
        logger.error("%s: %s", label, reason)
        return report_item, None

    path = file_name
    if not os.path.isabs(path):
        path = os.path.normpath(os.path.join(base_dir, file_name))

    # 1. 读取与格式校验（缺失 / 非 PCM / 非 16 位单声道 -> 错误）
    try:
        src_rate, samples = read_pcm16_mono_wav(path)
    except ValueError as exc:
        report_item["reason"] = str(exc)
        logger.error("%s: %s", label, exc)
        return report_item, None

    # 2. 裁剪（非法 / 空 -> 跳过；越界截断）
    cropped, reason = crop_samples(samples, src_rate,
                                   entry["start_ms"], entry["end_ms"])
    if reason is not None:
        report_item["status"] = "跳过"
        report_item["reason"] = reason
        logger.info("%s: 跳过 - %s", label, reason)
        return report_item, None

    # 3. 重采样到目标采样率
    resampled = resample_linear(cropped, src_rate, target_rate)
    if len(resampled) == 0:
        report_item["status"] = "跳过"
        report_item["reason"] = "重采样后为空"
        logger.info("%s: 跳过 - 重采样后为空", label)
        return report_item, None

    # 4. 响度归一化
    normalized, reason = normalize_loudness(resampled, entry["target_dbfs"])
    if reason is not None:
        report_item["status"] = "跳过"
        report_item["reason"] = reason
        logger.info("%s: 跳过 - %s", label, reason)
        return report_item, None

    duration = len(normalized) / target_rate
    report_item["status"] = "成功"
    report_item["duration_seconds"] = round(duration, 6)
    measured = rms_dbfs(normalized)
    logger.info(
        "%s: 成功 - 源%dHz 裁剪%d帧 重采样到%dHz 共%d帧 时长%.6fs "
        "目标%.1fdBFS 实测%.2fdBFS",
        label, src_rate, len(cropped), target_rate, len(normalized),
        duration, entry["target_dbfs"], measured)
    return report_item, (normalized, target_rate, entry["silence_after_ms"])


def main(argv=None):
    # Windows 控制台默认 GBK，保证中文用法与输出不崩
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    logger = setup_logger(args.log)
    logger.info("开始处理, 清单=%s", args.manifest)

    manifest = load_manifest(args.manifest, logger)
    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    default_rate = manifest.get("target_sample_rate")

    report_items = []
    output_rate = default_rate
    rendered = []  # (samples, target_rate, silence_after_ms)

    for entry in manifest["segments"]:
        if not isinstance(entry, dict):
            item = {"file": None, "status": "错误",
                    "reason": "清单条目不是对象: %r" % (entry,),
                    "duration_seconds": None}
            logger.error("未知条目: %s", item["reason"])
            report_items.append(item)
            continue
        item, payload = process_segment(entry, base_dir, default_rate, logger)
        report_items.append(item)
        if payload is not None:
            rendered.append(payload)
            if output_rate is None:
                output_rate = payload[1]

    # 5. 拼接：成功片段 + 片段间静音；最后一条之后不插静音
    final_samples = array.array("h")
    silence_frames_total = 0
    for index, (samples, rate, silence_ms) in enumerate(rendered):
        final_samples.extend(samples)
        if index < len(rendered) - 1 and silence_ms > 0:
            gap_frames = int(round(silence_ms * rate / 1000.0))
            final_samples.extend(array.array("h", [0]) * gap_frames)
            silence_frames_total += gap_frames

    if output_rate is None:
        # 没有任何成功片段：输出空 WAV，采样率取清单默认或 44100 兜底
        output_rate = default_rate if isinstance(default_rate, int) else 44100
        logger.info("无成功片段, 输出空WAV, 采样率=%d", output_rate)

    write_pcm16_mono_wav(args.output, output_rate, final_samples)
    total_duration = len(final_samples) / output_rate
    logger.info("拼接完成: 成功片段%d个, 静音帧%d, 总帧数%d, 总时长%.6fs, 输出=%s",
                len(rendered), silence_frames_total, len(final_samples),
                total_duration, args.output)

    # 6. JSON 报告
    success_count = sum(1 for it in report_items if it["status"] == "成功")
    skip_count = sum(1 for it in report_items if it["status"] == "跳过")
    error_count = sum(1 for it in report_items if it["status"] == "错误")
    report = {
        "output_file": os.path.abspath(args.output),
        "sample_rate": output_rate,
        "channels": 1,
        "sample_width_bits": 16,
        "total_duration_seconds": round(total_duration, 6),
        "total_frames": len(final_samples),
        "summary": {
            "total": len(report_items),
            "success": success_count,
            "skipped": skip_count,
            "error": error_count,
        },
        "segments": report_items,
    }
    parent = os.path.dirname(os.path.abspath(args.report))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("报告已写出: %s", args.report)
    logger.info("处理结束: 共%d条 成功%d 跳过%d 错误%d 总时长%.6fs",
                len(report_items), success_count, skip_count,
                error_count, total_duration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
