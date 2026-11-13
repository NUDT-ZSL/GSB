# -*- coding: utf-8 -*-
"""现场录音素材拼接器（仅标准库）。

按剪辑清单 JSON 依次处理单声道 16 位 44.1kHz PCM WAV 片段：
裁剪 -> 去首尾静音 -> 增益(钳位) -> 倒放，拼接后整体归一化到满量程 90%，
导出成品 WAV 与逐段状态报告 JSON。

用法:
    python splicer.py <manifest.json>
清单格式:
    {
      "output": "out/final.wav",          # 可选，默认 manifest 同目录 final.wav
      "report": "out/report.json",        # 可选，默认 manifest 同目录 report.json
      "segments": [
        {"file": "a.wav", "start_ms": 0.0, "end_ms": 500.0,
         "reverse": false, "silence_threshold": 100, "gain": 1.5}
      ]
    }
"""
import json
import os
import struct
import sys
import time
import wave

SAMPLE_RATE = 44100
FULL_SCALE = 32768
NORMALIZE_TARGET = round(FULL_SCALE * 0.9)  # 29491
MIN_SAMPLE = -32768
MAX_SAMPLE = 32767
READ_ATTEMPTS = 3
READ_RETRY_DELAY = 0.05


class ManifestError(ValueError):
    """清单字段非法，阻止处理。"""


# ---------------------------------------------------------------- WAV 读写

def read_wav_samples(path):
    """读取 WAV，返回 (channels, sampwidth, framerate, samples[list[int]])。

    读取阶段不做格式校验，由调用方按规则判定跳过原因。
    """
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        vals = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    else:  # 非 16 位时仍尝试解出样本值，供报告统计；格式校验会先行拦截
        vals = []
    if nch > 1 and vals:
        vals = vals[::nch]  # 仅取第一声道占位（该段会被跳过，不进入成品）
    return nch, sw, fr, vals


def read_wav_with_retry(path, attempts=READ_ATTEMPTS, delay=READ_RETRY_DELAY):
    """读取 WAV，遇到暂时性 I/O 故障时重试；文件缺失不重试。"""
    last = None
    for _ in range(attempts):
        try:
            return read_wav_samples(path)
        except FileNotFoundError:
            raise
        except OSError as exc:  # 暂时性故障（占用、句柄冲突等），稍后重试
            last = exc
            time.sleep(delay)
    raise last


def write_wav(path, samples):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples) if samples else b"")


# ---------------------------------------------------------------- 清单校验

def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_manifest(manifest):
    """字段类型校验；非法时抛出 ManifestError，错误信息含字段路径。"""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("segments"), list):
        raise ManifestError("字段 segments 必须为数组")
    for i, seg in enumerate(manifest["segments"]):
        if not isinstance(seg, dict):
            raise ManifestError("字段 segments[%d] 必须为对象" % i)
        if "reverse" in seg and not isinstance(seg["reverse"], bool):
            raise ManifestError("字段 segments[%d].reverse 必须为布尔值" % i)
        if "silence_threshold" in seg and not _is_number(seg["silence_threshold"]):
            raise ManifestError("字段 segments[%d].silence_threshold 必须为数值" % i)
        if "gain" in seg and not _is_number(seg["gain"]):
            raise ManifestError("字段 segments[%d].gain 必须为数值" % i)
        for key in ("start_ms", "end_ms"):
            if key in seg and not _is_number(seg[key]):
                raise ManifestError("字段 segments[%d].%s 必须为数值" % (i, key))


# ---------------------------------------------------------------- 片段处理

def clamp16(v):
    if v > MAX_SAMPLE:
        return MAX_SAMPLE
    if v < MIN_SAMPLE:
        return MIN_SAMPLE
    return v


def trim_silence(samples, threshold):
    """去除首尾静音；绝对值等于阈值的样本视为静音。"""
    first = 0
    last = len(samples)
    while first < last and abs(samples[first]) <= threshold:
        first += 1
    while last > first and abs(samples[last - 1]) <= threshold:
        last -= 1
    return samples[first:last]


def process_segment_samples(samples, seg):
    """对已裁剪的样本依次执行：去静音 -> 增益(钳位) -> 倒放。"""
    out = list(samples)
    if "silence_threshold" in seg:
        out = trim_silence(out, seg["silence_threshold"])
    gain = seg.get("gain", 1.0)
    if gain != 1.0:
        out = [clamp16(int(round(s * gain))) for s in out]
    if seg.get("reverse", False):
        out.reverse()
    return out


def normalize(samples):
    """整体归一化：最大绝对样本达到满量程 90%（29491）；全零保持全零。"""
    if not samples:
        return samples
    peak = max(abs(s) for s in samples)
    if peak == 0:
        return list(samples)
    factor = NORMALIZE_TARGET / peak
    return [clamp16(int(round(s * factor))) for s in samples]


# ---------------------------------------------------------------- 主流程

def _ms(n_samples):
    return n_samples / SAMPLE_RATE * 1000.0


def run(manifest):
    """执行清单，返回报告 dict；全部片段无效时不生成成品 WAV。"""
    validate_manifest(manifest)
    segments = manifest["segments"]
    report_segments = []
    final_samples = []

    for idx, seg in enumerate(segments):
        entry = {
            "index": idx,
            "status": "skipped",
            "reason": None,
            "original_duration_ms": None,
            "processed_duration_ms": None,
        }
        report_segments.append(entry)
        path = seg.get("file", "")

        if not os.path.isfile(path):
            entry["reason"] = "文件缺失: %s" % path
            continue
        try:
            nch, sw, fr, samples = read_wav_with_retry(path)
        except FileNotFoundError:
            entry["reason"] = "文件缺失: %s" % path
            continue
        except (OSError, wave.Error, EOFError) as exc:
            entry["reason"] = "读取失败: %s" % exc
            continue

        if nch != 1:
            entry["reason"] = "非单声道(声道数=%d)" % nch
            continue
        if sw != 2:
            entry["reason"] = "非16位PCM(位深=%d)" % (sw * 8)
            continue
        if fr != SAMPLE_RATE:
            entry["reason"] = "采样率非44100Hz(实际=%d)" % fr
            continue

        duration_ms = _ms(len(samples))
        start_ms = seg.get("start_ms", 0.0)
        end_ms = seg.get("end_ms", duration_ms)
        if start_ms < 0 or end_ms > duration_ms + 1e-9:
            entry["reason"] = "起止时间越界(时长=%.1fms)" % duration_ms
            entry["original_duration_ms"] = duration_ms
            continue
        if start_ms >= end_ms:
            entry["reason"] = "起止时间倒置(start=%.1fms,end=%.1fms)" % (start_ms, end_ms)
            entry["original_duration_ms"] = duration_ms
            continue

        s0 = int(round(start_ms / 1000.0 * SAMPLE_RATE))
        s1 = min(int(round(end_ms / 1000.0 * SAMPLE_RATE)), len(samples))
        cropped = samples[s0:s1]
        entry["original_duration_ms"] = _ms(len(cropped))

        processed = process_segment_samples(cropped, seg)
        entry["status"] = "processed"
        entry["processed_duration_ms"] = _ms(len(processed))
        final_samples.extend(processed)

    processed_count = sum(1 for e in report_segments if e["status"] == "processed")
    skipped_count = len(report_segments) - processed_count
    output_path = manifest.get("output")
    report_path = manifest.get("report")

    report = {
        "success": processed_count > 0,
        "output_path": None,
        "summary": {
            "total_segments": len(report_segments),
            "processed": processed_count,
            "skipped": skipped_count,
            "output_path": None,
            "total_duration_ms": 0.0,
        },
        "segments": report_segments,
    }

    if processed_count == 0:
        report["error"] = "所有片段均无效，未生成成品"
        report["reasons"] = [e["reason"] for e in report_segments if e["reason"]]
        return report

    final_samples = normalize(final_samples)
    if output_path:
        write_wav(output_path, final_samples)
        report["output_path"] = output_path
        report["summary"]["output_path"] = output_path
    report["summary"]["total_duration_ms"] = _ms(len(final_samples))
    return report


def run_file(manifest_path):
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    base = os.path.dirname(os.path.abspath(manifest_path))
    manifest.setdefault("output", os.path.join(base, "final.wav"))
    manifest.setdefault("report", os.path.join(base, "report.json"))
    report = run(manifest)
    os.makedirs(os.path.dirname(os.path.abspath(manifest["report"])), exist_ok=True)
    with open(manifest["report"], "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main(argv):
    if len(argv) != 2:
        print("用法: python splicer.py <manifest.json>", file=sys.stderr)
        return 2
    try:
        report = run_file(argv[1])
    except ManifestError as exc:
        print("清单错误: %s" % exc, file=sys.stderr)
        return 2
    s = report["summary"]
    print("总片段=%d 成功=%d 跳过=%d 成品时长=%.1fms -> %s"
          % (s["total_segments"], s["processed"], s["skipped"],
             s["total_duration_ms"], report["output_path"]))
    if not report["success"]:
        print("整体失败: %s" % report.get("error", ""), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
