#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""播客采访剪切与响度对齐工具。

读取 JSON 剪切剧本，对单声道 16 位 PCM WAV 采访录音执行：
  1. 逐片段校验（必填字段、时间合法性、区间边界）；
  2. 按剧本顺序裁剪有效片段并做 RMS 响度对齐（峰值不超过 0 dBFS，
     所需增益超出 ±20 dB 判为响度异常并剔除）；
  3. 相邻片段间插入 20 ms 静音拼接，导出单声道 16 位 PCM WAV；
  4. 在输出 WAV 同目录生成 JSON 处理报告。

仅使用 Python 标准库，本地确定性运行。

用法：
    python podcast_cutter.py <script.json>

剧本格式：
{
  "input":  "interview.wav",          // 默认音频源（相对剧本所在目录）
  "output": "out/episode.wav",        // 输出 WAV（相对剧本所在目录）
  "report": "out/report.json",        // 可选，默认 <output同名>_report.json
  "clips": [
    {"clip": "intro", "start": 0.0, "end": 1.0, "target_loudness": -20.0,
     "source": "other.wav"}           // 可选：该片段单独指定音频源
  ]
}

退出码：0=成功/部分成功，1=全部片段失败，2=剧本被拒绝（字段缺失/非法时间）。
"""
import json
import math
import os
import sys
import wave
from array import array

REQUIRED_FIELDS = ("clip", "start", "end", "target_loudness")
SILENCE_MS = 20                 # 片段间静音（毫秒）
GAIN_LIMIT_DB = 20.0            # 响度异常判定阈值（±dB）
FULL_SCALE = 32768.0            # 16 位 PCM 满幅


class ScriptRejected(Exception):
    """剧本整体被拒绝（不生成输出音频，但仍写报告）。"""

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = problems


# ---------------------------------------------------------------- 音频 I/O

def load_wav(path):
    """读取单声道 16 位 PCM WAV，返回 (采样率, array('h') 样本)。"""
    with wave.open(path, "rb") as w:
        nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if nch != 1:
            raise ValueError("仅支持单声道 WAV，实际声道数=%d" % nch)
        if sw != 2:
            raise ValueError("仅支持 16 位 PCM WAV，实际样本宽度=%d 字节" % sw)
        raw = w.readframes(w.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    return sr, samples


def write_wav(path, sample_rate, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())


# ---------------------------------------------------------------- 电平计算

def rms_dbfs(samples):
    """样本段的 RMS 电平（dBFS）；全零段返回 -inf。"""
    if not samples:
        return float("-inf")
    acc = 0.0
    for s in samples:
        acc += s * s
    rms = math.sqrt(acc / len(samples)) / FULL_SCALE
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms)


def peak_dbfs(samples):
    if not samples:
        return float("-inf")
    peak = max(max(samples), -min(samples)) / FULL_SCALE
    if peak <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(peak)


def apply_gain(samples, gain_db):
    """施加增益并截断到 16 位范围（增益已按不削顶约束计算，截断仅为保险）。"""
    factor = 10.0 ** (gain_db / 20.0)
    out = array("h", bytes(2 * len(samples)))
    for i, s in enumerate(samples):
        v = int(round(s * factor))
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = v
    return out


# ---------------------------------------------------------------- 剧本校验

def validate_script(script):
    """整体校验剧本。缺失必填字段或非法时间值 -> ScriptRejected。"""
    problems = []
    if not isinstance(script, dict):
        raise ScriptRejected(["剧本顶层必须是 JSON 对象"])
    clips = script.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ScriptRejected(["剧本缺少非空 clips 数组"])
    if not script.get("input"):
        problems.append("缺少顶层 input 音频路径")
    if not script.get("output"):
        problems.append("缺少顶层 output 输出路径")

    for idx, clip in enumerate(clips):
        if not isinstance(clip, dict):
            problems.append("片段[%d] 不是 JSON 对象" % idx)
            continue
        # 必填字段（要求 1）
        missing = [f for f in REQUIRED_FIELDS if f not in clip]
        if missing:
            problems.append("片段[%d] 缺失必填字段: %s"
                            % (idx, ", ".join(missing)))
            continue
        # 时间合法性（要求 11）：必须为数字且非负
        for key in ("start", "end"):
            v = clip[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                problems.append("片段[%d] 非法时间值: %s=%r（非数字）"
                                % (idx, key, v))
            elif v < 0:
                problems.append("片段[%d] 非法时间值: %s=%r（负值）"
                                % (idx, key, v))
        # 目标响度必须为数字
        tl = clip["target_loudness"]
        if isinstance(tl, bool) or not isinstance(tl, (int, float)):
            problems.append("片段[%d] 非法目标响度: target_loudness=%r（非数字）"
                            % (idx, tl))
    if problems:
        raise ScriptRejected(problems)


# ---------------------------------------------------------------- 主流程

def process(script_path):
    script_path = os.path.abspath(script_path)
    base_dir = os.path.dirname(script_path)
    errors = []
    results = []

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    def resolve(p):
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        return os.path.normpath(p)

    output_path = resolve(script.get("output", "output.wav"))
    report_path = resolve(script.get(
        "report", os.path.splitext(output_path)[0] + "_report.json"))

    def write_report(status, unprocessed, extra=None):
        report = {
            "status": status,               # success / partial / failed / rejected
            "script": script_path,
            "output": output_path,
            "overwritten": False,
            "unprocessed_indices": unprocessed,
            "results": results,
            "errors": errors,
        }
        if extra:
            report.update(extra)
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        return report

    # ---- 整体校验（要求 1、11）：失败则不生成输出音频
    try:
        validate_script(script)
    except ScriptRejected as e:
        errors.extend(e.problems)
        write_report("rejected", list(range(len(script.get("clips", []) or []))))
        return 2, report_path

    clips = script["clips"]
    default_source = resolve(script["input"])
    source_cache = {}

    def get_source(path):
        if path not in source_cache:
            source_cache[path] = load_wav(path)   # 可能抛异常，由调用方捕获
        return source_cache[path]

    produced = []        # 已产出的片段样本
    unprocessed = []
    sample_rate = None

    for idx, clip in enumerate(clips):
        name = clip["clip"]
        entry = {"index": idx, "clip": name, "status": None,
                 "duration_s": None, "gain_db": None}
        results.append(entry)

        # ---- 读取音频源（要求 6）：失败则跳过该片段，继续其余
        src_path = resolve(clip["source"]) if "source" in clip else default_source
        try:
            sr, samples = get_source(src_path)
        except Exception as exc:  # 文件缺失/损坏/格式不支持
            reason = "音频读取失败: %s (%s)" % (src_path, exc)
            entry.update(status="skipped", duration_s=0.0, reason=reason)
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            unprocessed.append(idx)
            continue

        audio_dur = len(samples) / sr
        start, end = float(clip["start"]), float(clip["end"])
        entry["duration_s"] = round(end - start, 3)

        # ---- 区间校验（要求 2）
        if not start < end:
            reason = "无效区间: start(%.3f) 必须小于 end(%.3f)" % (start, end)
            entry.update(status="skipped", reason=reason)
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            unprocessed.append(idx)
            continue
        if end > audio_dur:
            reason = ("区间越界: end(%.3f) 超出音频时长(%.3f)"
                      % (end, audio_dur))
            entry.update(status="skipped", reason=reason)
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            unprocessed.append(idx)
            continue

        seg = samples[round(start * sr):round(end * sr)]
        target = float(clip["target_loudness"])
        rms = rms_dbfs(seg)
        peak = peak_dbfs(seg)
        entry["rms_dbfs"] = None if math.isinf(rms) else round(rms, 3)
        entry["target_loudness"] = target

        # ---- 响度异常（要求 10）：所需增益超出 ±20 dB
        gain = target - rms
        if math.isinf(gain) or gain > GAIN_LIMIT_DB or gain < -GAIN_LIMIT_DB:
            reason = ("响度异常: 片段RMS=%s dBFS，达到目标 %.1f dBFS 所需增益 %s dB "
                      "超出 ±%.0f dB 限制"
                      % (("-inf" if math.isinf(rms) else "%.2f" % rms),
                         target,
                         ("+inf" if math.isinf(gain) else "%.2f" % gain),
                         GAIN_LIMIT_DB))
            entry.update(status="skipped", gain_db=None, reason=reason)
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            unprocessed.append(idx)
            continue

        # ---- 峰值约束（要求 4、5）：目标不可达则不施加过载增益
        status = "ok"
        if peak + gain > 0.0:
            capped = -peak  # 峰值恰好对齐 0 dBFS 的最大安全增益
            reason = ("目标响度 %.1f dBFS 不可达: 片段峰值 %.2f dBFS，"
                      "所需增益 %.2f dB 将使峰值超过 0 dBFS；"
                      "已限制增益为 %.2f dB 以避免削顶"
                      % (target, peak, gain, capped))
            entry["reason"] = reason
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            gain = capped
            status = "capped"

        gained = apply_gain(seg, gain)
        entry.update(status=status, gain_db=round(gain, 3),
                     output_rms_dbfs=round(rms_dbfs(gained), 3),
                     output_peak_dbfs=round(peak_dbfs(gained), 3))
        produced.append(gained)
        if sample_rate is None:
            sample_rate = sr
        elif sr != sample_rate:
            # 多源采样率不一致：该片段无法无损拼接，剔除
            produced.pop()
            reason = ("采样率不一致: 片段为 %d Hz，输出为 %d Hz"
                      % (sr, sample_rate))
            entry.update(status="skipped", gain_db=None, reason=reason)
            errors.append("片段[%d] %s: %s" % (idx, name, reason))
            unprocessed.append(idx)
            continue

    # ---- 拼接与导出（要求 3、8、9）
    if produced:
        silence = array("h", bytes(2 * round(sample_rate * SILENCE_MS / 1000.0)))
        out = array("h")
        for i, seg in enumerate(produced):
            if i:
                out.extend(silence)
            out.extend(seg)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        overwritten = os.path.exists(output_path)
        write_wav(output_path, sample_rate, out)
        status = "success" if not unprocessed else "partial"
    else:
        out = None
        overwritten = False
        status = "failed"

    report = write_report(status, unprocessed, extra={
        "sample_rate": sample_rate,
        "silence_ms": SILENCE_MS,
        "output_duration_s": (round(len(out) / sample_rate, 3)
                              if out is not None else 0.0),
    })
    # 覆盖记录（要求 9）
    if overwritten:
        report["overwritten"] = True
        report.setdefault("notes", []).append(
            "输出文件已存在，已覆盖: %s" % output_path)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    return (0 if status in ("success", "partial") else 1), report_path


def main(argv):
    if len(argv) != 2:
        print("用法: python podcast_cutter.py <script.json>", file=sys.stderr)
        return 2
    code, report_path = process(argv[1])
    with open(report_path, encoding="utf-8") as f:
        report = json.load(f)
    print("状态: %s" % report["status"])
    print("报告: %s" % report_path)
    for line in report["errors"]:
        print("  - %s" % line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
