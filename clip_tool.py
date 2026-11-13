# -*- coding: utf-8 -*-
"""
clip_tool.py — 游戏对白 WAV 批量剪辑与波形统计工具（仅依赖 Python 标准库）

功能：
  1. 读取 config.json（输入/输出目录 + 每条处理指令：文件名、起止秒数、增益倍数）
  2. 仅接受单声道、16 位 PCM WAV；立体声/8 位/非 PCM/损坏文件在报告中记录错误，
     不影响其余文件处理
  3. 按起止秒数截取片段（帧对齐，输出时长误差 <= 1 个采样周期）
  4. 应用增益倍数，超范围样本钳制到 [-32768, 32767]，绝不回绕溢出
  5. 输出标准 PCM WAV（RIFF/fmt/data 块，可被 wave 模块重新打开），
     通道数/位深/采样率与输入一致
  6. 对每个成功片段统计峰值(peak)、RMS、过零率(zcr)，写入 JSON 报告
  7. 报告按输出文件名排序；文件缺失、时间段越界等均记为错误
  8. 输出目录自动创建；输出文件基于原名与片段序号命名，重名时追加序号不覆盖
  9. 处理日志写文件，含每个文件的开始/结果行及末尾成功/错误汇总
 10. 命令行入口：python clip_tool.py config.json，控制台打印成功/错误汇总

退出码：0 = 批处理正常结束（允许部分/全部文件错误）；2 = 配置等致命错误
"""

import json
import logging
import math
import os
import struct
import sys
import wave

# 16 位有符号 PCM 的取值边界
INT16_MIN = -32768
INT16_MAX = 32767


def _force_utf8_stdio():
    """让控制台输出在中文 Windows(GBK) 下也稳定使用 UTF-8，避免编码崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------- #
# 配置读取与校验
# --------------------------------------------------------------------------- #
def _is_number(value):
    """布尔值是 int 的子类，配置里的数值不接受 bool。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_config(config_path):
    """读取并校验配置文件，返回规范化后的 dict。"""
    with open(config_path, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    if not isinstance(cfg, dict):
        raise ValueError("配置根节点必须是 JSON 对象")

    input_dir = cfg.get("input_dir")
    output_dir = cfg.get("output_dir")
    if not isinstance(input_dir, str) or not input_dir.strip():
        raise ValueError("配置缺少有效的 input_dir（字符串）")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError("配置缺少有效的 output_dir（字符串）")

    tasks_raw = cfg.get("tasks")
    if not isinstance(tasks_raw, list) or len(tasks_raw) == 0:
        raise ValueError("配置 tasks 必须是非空数组")

    tasks = []
    for idx, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            raise ValueError("tasks[%d] 必须是对象" % idx)

        filename = item.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("tasks[%d].filename 必须是非空字符串" % idx)

        start = item.get("start_sec")
        end = item.get("end_sec")
        gain = item.get("gain")
        if not _is_number(start) or start < 0:
            raise ValueError("tasks[%d].start_sec 必须是非负数" % idx)
        if not _is_number(end) or end <= 0:
            raise ValueError("tasks[%d].end_sec 必须是正数" % idx)
        # 注意：end <= start 不在此处致命退出，留给逐条处理阶段记为错误条目
        if not _is_number(gain) or gain < 0 or not math.isfinite(gain):
            raise ValueError("tasks[%d].gain 必须是非负有限数" % idx)

        tasks.append(
            {
                "filename": filename,
                "start_sec": float(start),
                "end_sec": float(end),
                "gain": float(gain),
            }
        )

    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "report_name": cfg.get("report_name", "report.json"),
        "log_name": cfg.get("log_name", "clip_tool.log"),
        "tasks": tasks,
    }


# --------------------------------------------------------------------------- #
# 输出文件命名（原名 + 片段序号；已存在则继续追加序号，绝不覆盖）
# --------------------------------------------------------------------------- #
def resolve_output_path(output_dir, base_name, clip_index, planned):
    """
    生成输出路径：
      首选  <base>_clip<N>.wav
      若磁盘已存在或本批次已计划使用，则依次尝试 _clip<N>_1 / _clip<N>_2 ...
    """
    candidate = os.path.join(output_dir, "%s_clip%d.wav" % (base_name, clip_index))
    suffix = 1
    while os.path.exists(candidate) or candidate in planned:
        candidate = os.path.join(
            output_dir, "%s_clip%d_%d.wav" % (base_name, clip_index, suffix)
        )
        suffix += 1
    planned.add(candidate)
    return candidate


# --------------------------------------------------------------------------- #
# 波形统计
# --------------------------------------------------------------------------- #
def compute_stats(samples):
    """对已处理的整数样本序列计算 peak / rms / zero_crossing_rate。

    peak 为带符号峰值（max(正样本, |min(负样本)|)），取值恒在 [-32768, 32767]
    样本域内；其绝对值即波形振幅峰值。
    """
    n = len(samples)
    if n == 0:
        return {"peak": 0, "rms": 0.0, "zero_crossing_rate": 0.0}

    max_pos = 0
    min_neg = 0
    sum_sq = 0
    for s in samples:
        if s > max_pos:
            max_pos = s
        if s < min_neg:
            min_neg = s
        sum_sq += s * s
    peak = max_pos if max_pos >= -min_neg else min_neg

    rms = math.sqrt(sum_sq / n)

    # 过零：相邻样本符号发生变化（0 视为正号侧，仅正负之间的跳变计数）
    crossings = 0
    prev = samples[0]
    for s in samples[1:]:
        if (prev < 0) != (s < 0):
            crossings += 1
        prev = s
    zcr = crossings / n

    return {
        "peak": int(peak),
        "rms": round(rms, 4),
        "zero_crossing_rate": round(zcr, 8),
    }


# --------------------------------------------------------------------------- #
# 单条指令处理（任何异常都被捕获并转成错误条目，不影响其他文件）
# --------------------------------------------------------------------------- #
def process_task(task, input_dir, output_dir, clip_index, planned):
    entry = {
        "filename": task["filename"],
        "clip_index": clip_index,
        "start_sec": task["start_sec"],
        "end_sec": task["end_sec"],
        "gain": task["gain"],
        "status": "error",
        "error": None,
        "message": "",
        "output_file": None,
        "sample_rate": None,
        "channels": None,
        "sample_width_bits": None,
        "frame_count": 0,
        "duration_sec": None,
        "stats": None,
    }

    src_path = os.path.join(input_dir, task["filename"])
    base_name = os.path.splitext(os.path.basename(task["filename"]))[0]

    if not os.path.isfile(src_path):
        entry["error"] = "FileNotFound"
        entry["message"] = "输入文件不存在: %s" % task["filename"]
        return entry

    try:
        with wave.open(src_path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()          # 字节数
            sample_rate = wf.getframerate()
            compression = wf.getcomptype()
            total_frames = wf.getnframes()

            # 仅接受单声道 16 位 PCM
            if compression != "NONE":
                entry["error"] = "UnsupportedFormat"
                entry["message"] = "仅支持 PCM(NONE) 格式，当前压缩类型: %s" % compression
                return entry
            if channels != 1:
                entry["error"] = "UnsupportedFormat"
                entry["message"] = "仅支持单声道文件，当前通道数: %d" % channels
                return entry
            if sample_width != 2:
                entry["error"] = "UnsupportedFormat"
                entry["message"] = (
                    "仅支持 16 位 PCM，当前位深: %d 位" % (sample_width * 8)
                )
                return entry

            # 秒 -> 帧（四舍五入），并做越界检查
            start_frame = int(round(task["start_sec"] * sample_rate))
            end_frame = int(round(task["end_sec"] * sample_rate))
            if start_frame < 0 or start_frame > total_frames:
                entry["error"] = "TimeRangeOutOfBounds"
                entry["message"] = (
                    "起始帧 %d 越界（文件总帧数 %d）" % (start_frame, total_frames)
                )
                return entry
            if end_frame > total_frames:
                entry["error"] = "TimeRangeOutOfBounds"
                entry["message"] = (
                    "结束帧 %d 越界（文件总帧数 %d）" % (end_frame, total_frames)
                )
                return entry
            if end_frame <= start_frame:
                entry["error"] = "TimeRangeOutOfBounds"
                entry["message"] = (
                    "截取片段为空（起始帧 %d，结束帧 %d）" % (start_frame, end_frame)
                )
                return entry

            frame_count = end_frame - start_frame
            wf.setpos(start_frame)
            raw = wf.readframes(frame_count)
    except wave.Error as exc:
        entry["error"] = "InvalidWav"
        entry["message"] = "WAV 文件无法解析或已损坏: %s" % exc
        return entry
    except OSError as exc:
        entry["error"] = "IOError"
        entry["message"] = "读取文件失败: %s" % exc
        return entry

    # 实际读到的字节数应与帧数匹配；不匹配说明文件数据块损坏
    if len(raw) != frame_count * 2:
        entry["error"] = "InvalidWav"
        entry["message"] = (
            "data 块数据不完整：期望 %d 字节，实际 %d 字节"
            % (frame_count * 2, len(raw))
        )
        return entry

    # 解码 16 位有符号小端样本 -> 增益 -> 钳制（杜绝回绕）
    in_samples = struct.unpack("<%dh" % frame_count, raw)
    gain = task["gain"]
    out_samples = [0] * frame_count
    for i, s in enumerate(in_samples):
        scaled = int(round(s * gain))
        if scaled > INT16_MAX:
            scaled = INT16_MAX
        elif scaled < INT16_MIN:
            scaled = INT16_MIN
        out_samples[i] = scaled

    out_path = resolve_output_path(output_dir, base_name, clip_index, planned)

    try:
        with wave.open(out_path, "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(sample_rate)
            out_wf.writeframes(struct.pack("<%dh" % frame_count, *out_samples))
    except OSError as exc:
        entry["error"] = "IOError"
        entry["message"] = "写出 WAV 失败: %s" % exc
        return entry

    stats = compute_stats(out_samples)
    duration = frame_count / sample_rate

    entry.update(
        {
            "status": "success",
            "error": None,
            "message": "OK",
            "output_file": os.path.basename(out_path),
            "sample_rate": sample_rate,
            "channels": 1,
            "sample_width_bits": 16,
            "frame_count": frame_count,
            "duration_sec": round(duration, 6),
            "stats": stats,
        }
    )
    return entry


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv):
    _force_utf8_stdio()
    if len(argv) != 2:
        sys.stderr.write("用法: python clip_tool.py <config.json>\n")
        return 2

    config_path = argv[1]
    if not os.path.isfile(config_path):
        sys.stderr.write("致命错误: 配置文件不存在: %s\n" % config_path)
        return 2

    try:
        cfg = load_config(config_path)
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        sys.stderr.write("致命错误: 配置读取失败: %s\n" % exc)
        return 2

    # 相对路径以配置文件所在目录为基准，保证任意工作目录下行为一致
    cfg_dir = os.path.dirname(os.path.abspath(config_path))
    input_dir = cfg["input_dir"]
    output_dir = cfg["output_dir"]
    if not os.path.isabs(input_dir):
        input_dir = os.path.join(cfg_dir, input_dir)
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(cfg_dir, output_dir)

    os.makedirs(output_dir, exist_ok=True)

    log_path = os.path.join(output_dir, cfg["log_name"])
    report_path = os.path.join(output_dir, cfg["report_name"])
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    logger = logging.getLogger("clip_tool")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    logger.info("批处理开始：配置=%s 输入目录=%s 输出目录=%s",
                config_path, input_dir, output_dir)

    # 同一源文件的片段序号（按指令顺序计数，失败指令同样占号）
    per_file_counters = {}
    planned = set()
    entries = []

    for task in cfg["tasks"]:
        filename = task["filename"]
        clip_index = per_file_counters.get(filename, 0) + 1
        per_file_counters[filename] = clip_index

        logger.info("开始处理: %s [片段#%d] start=%.3fs end=%.3fs gain=%.3f",
                    filename, clip_index,
                    task["start_sec"], task["end_sec"], task["gain"])
        try:
            entry = process_task(task, input_dir, output_dir, clip_index, planned)
        except Exception as exc:  # 兜底：任何意外都不能拖垮整批
            entry = {
                "filename": filename,
                "clip_index": clip_index,
                "start_sec": task["start_sec"],
                "end_sec": task["end_sec"],
                "gain": task["gain"],
                "status": "error",
                "error": "UnexpectedError",
                "message": "未预期异常: %r" % exc,
                "output_file": None,
                "sample_rate": None,
                "channels": None,
                "sample_width_bits": None,
                "frame_count": 0,
                "duration_sec": None,
                "stats": None,
            }
        entries.append(entry)

        if entry["status"] == "success":
            logger.info("结果: 成功 %s -> %s 时长=%.6fs 帧数=%d peak=%d rms=%.4f zcr=%.8f",
                        filename, entry["output_file"],
                        entry["duration_sec"], entry["frame_count"],
                        entry["stats"]["peak"], entry["stats"]["rms"],
                        entry["stats"]["zero_crossing_rate"])
        else:
            logger.info("结果: 错误 %s [%s] %s",
                        filename, entry["error"], entry["message"])

    # 报告按输出文件名排序；失败条目没有输出文件，退回用源文件名+片段序号占位
    def sort_key(e):
        name = e["output_file"] or (
            "%s_clip%d.wav" % (os.path.splitext(e["filename"])[0], e["clip_index"])
        )
        return (name.lower(), e["clip_index"])

    entries.sort(key=sort_key)

    success_count = sum(1 for e in entries if e["status"] == "success")
    error_count = len(entries) - success_count

    report = {
        "summary": {
            "total": len(entries),
            "success": success_count,
            "error": error_count,
        },
        "entries": entries,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("汇总: 成功 %d 个，错误 %d 个，共 %d 个",
                success_count, error_count, len(entries))
    logger.info("批处理结束：报告=%s 日志=%s", report_path, log_path)

    # 控制台汇总
    print("处理完成: 成功 %d 个，错误 %d 个，共 %d 个"
          % (success_count, error_count, len(entries)))
    print("Summary: success=%d error=%d total=%d"
          % (success_count, error_count, len(entries)))
    print("报告: %s" % report_path)
    print("日志: %s" % log_path)

    logging.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
