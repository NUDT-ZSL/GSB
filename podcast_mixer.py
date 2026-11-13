#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
podcast_mixer.py —— 双人播客后期拼接工具（仅使用 Python 标准库）

功能概述
--------
1. 读取两轨 16bit PCM WAV，严格校验 RIFF/WAVE 结构，损坏/非 WAV 明确报错且不产出半成品。
2. 采样率不一致时线性重采样到较高采样率，报告记录每轨原始/目标采样率。
3. 多声道输入仅取第一声道（降混为单声道），报告标注。
4. 按起始/结束偏移截取时间线，越界偏移截断到有效范围并在报告注明。
5. 压缩超过阈值的连续静音为可配置短停顿，报告记录每次跳过的起止采样。
6. 每个语音段独立做峰值归一化；静音段不参与，全静音安全处理（防除零）。
7. 重叠片段相加采用饱和限幅，绝不发生 16bit 溢出回绕，限幅事件写入报告。
8. 依据标记文件为成品每段标注来源主播与标签（JSON 报告）。
9. 标记文件非法/越界行跳过并在 skipped 列表说明原因，合法标记不受影响。
10. 全静音/极短轨标注为无效，另一轨可独立产出成品。
11. 输出 WAV 头部与数据长度一致、16bit 单声道，写出后读回校验。
12. 同名输出默认拒绝覆盖，--overwrite 显式开启后方可写入。
13. 报告每段含起止采样、来源轨、标签、动作类型、跳过原因；无标签为 null。
14. segments 严格按成品时间线升序，跳过记录单独成列。
15. 失败时清理临时文件、退出码非零；成功退出码 0，目录仅一个 WAV + 一个 JSON。

标记文件格式（每行）
--------------------
    起始采样  结束采样  来源轨(A|B)  [标签文本...]
采样坐标统一为“成品目标采样率”下的采样序号。标签可省略，省略时为 null。
以 # 开头的行与空行会被忽略。
"""
from __future__ import annotations

import argparse
import array
import json
import os
import sys
import tempfile

# ----------------------------- 常量与异常 -----------------------------

TARGET_PEAK_DEFAULT = 28000          # 归一化目标峰值（16bit 满量程 32767）
DEFAULT_TARGET_RATE = 44100          # 默认目标采样率，两轨不一致时仍取较高者
SILENCE_THRESHOLD_DEFAULT = 500      # |样本| <= 该值视为静音
MIN_SILENCE_DEFAULT = 4410           # 连续静音至少 0.1s(@44.1k) 才压缩
PAUSE_SAMPLES_DEFAULT = 13230        # 压缩后保留 0.3s(@44.1k) 短停顿
MIN_VOICE_FRAMES_DEFAULT = 256       # 极短片段判定阈值

INT16_MIN = -32768
INT16_MAX = 32767


class MixerError(Exception):
    """业务层错误基类，CLI 捕获后以退出码 1 返回。"""


class AudioFormatError(MixerError):
    """输入文件不是合法 WAV 或 WAV 头损坏。"""


# ----------------------------- WAV 读取校验 -----------------------------

def read_wav(path: str) -> dict:
    """读取并严格校验 16bit PCM WAV，仅取第一声道。

    返回 dict:
      path, sample_rate, channels, downmixed_to_mono,
      frame_count, samples(array('h')), data_bytes_len
    """
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as exc:
        raise MixerError(f"无法打开输入文件 {path}: {exc}") from exc

    if len(blob) < 44 or blob[0:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise AudioFormatError(
            f"无法识别音频文件 {path}：不是 RIFF/WAVE 格式（文件可能是文本或已损坏）"
        )

    # 逐块解析 RIFF 结构，校验块长度，防止头损坏被静默接受
    pos = 12
    fmt_blob = None
    audio_blob = b""
    while pos + 8 <= len(blob):
        chunk_id = blob[pos:pos + 4]
        chunk_size = int.from_bytes(blob[pos + 4:pos + 8], "little")
        body_start = pos + 8
        body_end = body_start + chunk_size
        if body_end > len(blob):
            raise AudioFormatError(
                f"无法识别音频文件 {path}：WAVE 块 {chunk_id!r} 长度越界，文件头损坏"
            )
        if chunk_id == b"fmt ":
            fmt_blob = blob[body_start:body_end]
        elif chunk_id == b"data":
            audio_blob = blob[body_start:body_end]
        pos = body_end + (chunk_size & 1)  # 块按字对齐，奇数长度补 1 字节

    if fmt_blob is None:
        raise AudioFormatError(f"无法识别音频文件 {path}：缺少 fmt 块，文件头损坏")
    if len(fmt_blob) < 16:
        raise AudioFormatError(f"无法识别音频文件 {path}：fmt 块过短，文件头损坏")

    format_tag = int.from_bytes(fmt_blob[0:2], "little")
    channels = int.from_bytes(fmt_blob[2:4], "little")
    sample_rate = int.from_bytes(fmt_blob[4:8], "little")
    byte_rate = int.from_bytes(fmt_blob[8:12], "little")
    block_align = int.from_bytes(fmt_blob[12:14], "little")
    bits_per_sample = int.from_bytes(fmt_blob[14:16], "little")

    if format_tag != 1:
        raise AudioFormatError(
            f"无法识别音频文件 {path}：仅支持 PCM(format=1)，实际 format_tag={format_tag}"
        )
    if bits_per_sample != 16:
        raise AudioFormatError(
            f"无法识别音频文件 {path}：仅支持 16 位 PCM，实际 {bits_per_sample} 位"
        )
    if channels < 1 or channels > 2:
        raise AudioFormatError(
            f"无法识别音频文件 {path}：仅支持单声道/立体声，实际 {channels} 声道"
        )
    if sample_rate <= 0:
        raise AudioFormatError(f"无法识别音频文件 {path}：采样率非法 {sample_rate}")
    if byte_rate != sample_rate * channels * 2 or block_align != channels * 2:
        raise AudioFormatError(f"无法识别音频文件 {path}：fmt 块字节率/块对齐字段自相矛盾，头损坏")
    if len(audio_blob) != channels * (len(audio_blob) // (channels * 2)) * 2:
        raise AudioFormatError(f"无法识别音频文件 {path}：data 块长度与声道数不匹配，文件损坏")

    raw = array.array("h")
    try:
        raw.frombytes(audio_blob)
    except ValueError as exc:
        raise AudioFormatError(f"无法识别音频文件 {path}：data 块无法解析为 16bit PCM：{exc}") from exc
    if sys.byteorder != "little":  # WAV 小端，x86 平台通常无需交换
        raw.byteswap()

    frame_count = len(raw) // channels
    # 仅取第一声道（交错存储，步长为声道数）
    samples = raw[0::channels] if channels > 1 else array.array("h", raw)

    return {
        "path": path,
        "sample_rate": sample_rate,
        "channels": channels,
        "downmixed_to_mono": channels > 1,
        "frame_count": frame_count,
        "samples": samples,
        "data_bytes_len": len(audio_blob),
    }


# ----------------------------- 重采样 -----------------------------

def resample_linear(samples: array.array, src_rate: int, dst_rate: int) -> array.array:
    """线性插值重采样到目标采样率（src==dst 时原样返回拷贝）。

    输出帧数按“输入帧数 × 目标/源采样率”的整数比例换算，
    保证 22050→44100 这类整数倍转换帧数严格翻倍，不产生 ±1 抖动。
    整数比例上采样走精确分支：半采样点做线性插值，零帧与非零帧交界
    直接取零，静音区长度与源信号严格成比例，不发生边界渗入。
    """
    if src_rate == dst_rate:
        return array.array("h", samples)
    if len(samples) == 0:
        return array.array("h")

    n = len(samples)
    # 先按整数比例消去公共因子，再回退到四舍五入，保证整数倍严格精确
    n_out = (n * dst_rate) // src_rate
    if n_out * src_rate != n * dst_rate:
        n_out = int(round(n * dst_rate / src_rate))

    # 整数比例上采样精确分支（dst_rate 是 src_rate 的整数倍）
    if dst_rate % src_rate == 0:
        factor = dst_rate // src_rate
        out = array.array("h")
        append = out.append
        for i0 in range(n - 1):
            s0 = samples[i0]
            s1 = samples[i0 + 1]
            append(int(s0))
            for k in range(1, factor):
                frac = k / factor
                # 最近的源样本为零帧时输出归零，静音区长度与源严格成比例
                if (frac <= 0.5 and s0 == 0) or (frac > 0.5 and s1 == 0):
                    append(0)
                else:
                    append(int(round(s0 + (s1 - s0) * frac)))
        # 最后一个源样本展开为 factor 个输出（含 0..factor-1 个相位）
        last = int(samples[n - 1])
        for _ in range(factor):
            append(last)
        return out

    ratio = src_rate / dst_rate
    out = array.array("h")
    append = out.append
    for i in range(n_out):
        x = i * ratio
        i0 = int(x)
        if i0 >= n - 1:
            append(int(samples[n - 1]))
            continue
        s0 = samples[i0]
        s1 = samples[i0 + 1]
        frac = x - i0
        # 静音边界保护：插值跨越“零帧↔非零帧”时，落在零帧一侧的输出归零
        if s0 == 0 and s1 != 0 and frac < 0.5:
            append(0)
            continue
        if s1 == 0 and s0 != 0 and frac >= 0.5:
            append(0)
            continue
        v = s0 + (s1 - s0) * frac
        append(int(round(v)))
    return out


# ----------------------------- 偏移截断 -----------------------------

def clamp_offsets(start, end, n: int):
    """把起始/结束偏移截断到 [0, n]，返回 (start, end, warnings)。"""
    warnings = []
    if start is None:
        start = 0
    if end is None:
        end = n
    orig_start, orig_end = start, end
    if start < 0:
        warnings.append(f"起始偏移 {orig_start} 为负，截断为 0")
        start = 0
    if end < 0:
        warnings.append(f"结束偏移 {orig_end} 为负，截断为 0")
        end = 0
    if start > n:
        warnings.append(f"起始偏移 {orig_start} 超出有效采样数 {n}，截断为 {n}")
        start = n
    if end > n:
        warnings.append(f"结束偏移 {orig_end} 超出有效采样数 {n}，截断为 {n}")
        end = n
    if end < start:
        warnings.append(
            f"结束偏移 {orig_end} 小于起始偏移 {orig_start}，有效区间为空"
        )
        end = start
    return start, end, warnings


# ----------------------------- 静音压缩 -----------------------------

def compress_silence(samples: array.array, threshold: int, min_silence: int,
                     pause_len: int):
    """识别长静音并压缩。

    返回 (pieces, cuts):
      pieces: [{"kind":"speech"|"pause", "in_start","in_end","samples":array('h')}]
      cuts:   每次长静音的原始起止采样、长度、保留长度、位置(leading/internal/trailing)
    短于 min_silence 的静音视为语音内容保留；首尾长静音直接裁掉。
    """
    n = len(samples)
    long_runs = []
    i = 0
    while i < n:
        if abs(samples[i]) <= threshold:
            j = i
            while j < n and abs(samples[j]) <= threshold:
                j += 1
            if j - i >= min_silence:
                long_runs.append((i, j))
            i = j
        else:
            i += 1

    pieces = []
    cuts = []
    cursor = 0
    for run_start, run_end in long_runs:
        if run_start > cursor:  # 两段长静音之间（或开头）的语音内容
            pieces.append({
                "kind": "speech",
                "in_start": cursor,
                "in_end": run_start,
                "samples": array.array("h", samples[cursor:run_start]),
            })
        run_len = run_end - run_start
        if run_start == 0 or run_end == n:
            kept = 0
            position = "leading" if run_start == 0 else "trailing"
        else:
            kept = min(pause_len, run_len)
            position = "internal"
        cuts.append({
            "input_start": run_start,
            "input_end": run_end,
            "length": run_len,
            "kept_samples": kept,
            "position": position,
        })
        if kept > 0:
            pieces.append({
                "kind": "pause",
                "in_start": run_start,
                "in_end": run_end,
                "samples": array.array("h", [0]) * kept,
            })
        cursor = run_end
    if cursor < n:
        pieces.append({
            "kind": "speech",
            "in_start": cursor,
            "in_end": n,
            "samples": array.array("h", samples[cursor:n]),
        })
    return pieces, cuts


# ----------------------------- 峰值归一化 -----------------------------

def normalize_samples(samples: array.array, target_peak: int):
    """峰值归一化。全静音（peak==0）原样返回，杜绝除零。

    返回 (out, peak_before, peak_after, gain)。
    """
    peak = 0
    for v in samples:
        a = -v if v < 0 else v
        if a > peak:
            peak = a
    if peak == 0:
        return array.array("h", samples), 0, 0, 1.0
    gain = target_peak / peak
    out = array.array("h")
    for v in samples:
        s = int(round(v * gain))
        if s > INT16_MAX:
            s = INT16_MAX
        elif s < INT16_MIN:
            s = INT16_MIN
        out.append(s)
    peak_after = 0
    for v in out:
        a = -v if v < 0 else v
        if a > peak_after:
            peak_after = a
    return out, peak, peak_after, gain


def _saturate(v: int) -> int:
    if v > INT16_MAX:
        return INT16_MAX
    if v < INT16_MIN:
        return INT16_MIN
    return v


# ----------------------------- 标记文件 -----------------------------

def parse_markers(path: str, timeline_len: int):
    """解析标记文件。

    返回 (markers, skipped_rows):
      markers: [{"start","end","source","label","line_no","raw"}]
      skipped_rows: 非法/越界行及原因（写入报告 skipped 列表）
    """
    markers = []
    skipped_rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        raise MixerError(f"无法打开标记文件 {path}: {exc}") from exc

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=3)
        entry = {"line_no": line_no, "raw": line}
        if len(parts) < 3:
            skipped_rows.append({**entry, "reason": "字段不足，至少需要 起始 结束 来源轨"})
            continue
        s_text, e_text, source = parts[0], parts[1], parts[2]
        label = parts[3].strip() if len(parts) == 4 else None
        try:
            m_start = int(s_text)
            m_end = int(e_text)
        except ValueError:
            skipped_rows.append({**entry, "reason": f"起止采样不是整数: '{s_text}'/'{e_text}'"})
            continue
        if m_start < 0 or m_end < 0:
            skipped_rows.append({**entry, "reason": f"采样序号为负({m_start},{m_end})，非法"})
            continue
        if m_start >= m_end:
            skipped_rows.append({**entry, "reason": f"起始采样 {m_start} 不小于结束采样 {m_end}"})
            continue
        if source not in ("A", "B"):
            skipped_rows.append({**entry, "reason": f"来源轨 '{source}' 非法，仅支持 A/B"})
            continue
        if m_end > timeline_len or m_start >= timeline_len:
            skipped_rows.append({
                **entry,
                "reason": f"标记区间 [{m_start},{m_end}) 超出成品长度 {timeline_len}，越界",
            })
            continue
        markers.append({
            "start": m_start, "end": m_end, "source": source,
            "label": label, "line_no": line_no, "raw": line,
        })
    return markers, skipped_rows


# ----------------------------- WAV 写出与读回校验 -----------------------------

def build_wav_bytes(samples: array.array, sample_rate: int) -> bytes:
    data = samples.tobytes()
    pad = len(data) & 1
    riff_size = 36 + len(data) + pad
    header = b"RIFF" + riff_size.to_bytes(4, "little") + b"WAVE"
    fmt_chunk = (
        b"fmt " + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")                  # PCM
        + (1).to_bytes(2, "little")                  # 单声道
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 2).to_bytes(4, "little")    # 字节率
        + (2).to_bytes(2, "little")                  # 块对齐
        + (16).to_bytes(2, "little")                 # 位宽
    )
    data_chunk = b"data" + len(data).to_bytes(4, "little") + data
    return header + fmt_chunk + data_chunk + (b"\x00" if pad else b"")


def verify_wav(path: str, expected_frames: int, expected_rate: int) -> dict:
    """读回输出 WAV，校验头部字段、数据长度与位宽完全自洽。"""
    info = read_wav(path)
    if info["sample_rate"] != expected_rate:
        raise MixerError(
            f"输出校验失败：采样率 {info['sample_rate']} != 预期 {expected_rate}"
        )
    if info["channels"] != 1:
        raise MixerError(f"输出校验失败：声道数 {info['channels']} 不是单声道")
    if info["frame_count"] != expected_frames:
        raise MixerError(
            f"输出校验失败：读回帧数 {info['frame_count']} != 预期 {expected_frames}"
        )
    if info["data_bytes_len"] != expected_frames * 2:
        raise MixerError(
            f"输出校验失败：data 字节数 {info['data_bytes_len']} != 帧数*2 {expected_frames * 2}"
        )
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(4)
        riff_size = int.from_bytes(f.read(4), "little")
    file_size = os.path.getsize(path)
    if head != b"RIFF" or file_size != riff_size + 8:
        raise MixerError("输出校验失败：RIFF 头部尺寸字段与实际文件长度不一致")
    return info


# ----------------------------- 单轨处理流水线 -----------------------------

def process_track(label: str, path: str, start, end, target_rate: int,
                  threshold: int, min_silence: int, pause_len: int,
                  target_peak: int, min_voice: int):
    """读入 -> 校验 -> 取第一声道 -> 重采样 -> 偏移截断 -> 静音压缩 -> 归一化。"""
    wav = read_wav(path)
    original_rate = wav["sample_rate"]
    resampled = resample_linear(wav["samples"], original_rate, target_rate)
    n = len(resampled)

    s, e, warnings = clamp_offsets(start, end, n)
    region = array.array("h", resampled[s:e])

    track_report = {
        "track": label,
        "path": os.path.abspath(path),
        "original_sample_rate": original_rate,
        "target_sample_rate": target_rate,
        "original_channels": wav["channels"],
        "downmixed_to_mono": wav["downmixed_to_mono"],
        "original_frames": wav["frame_count"],
        "resampled_frames": n,
        "used_start": s,
        "used_end": e,
        "offset_warnings": warnings,
        "valid": True,
        "invalid_reason": None,
        "output_offset": None,
        "voice_piece_count": 0,
    }

    if len(region) == 0:
        track_report.update(valid=False, invalid_reason="偏移截断后有效区间为空")
        return {"report": track_report, "pieces": [], "cuts": [], "norm_records": []}
    if len(region) < min_voice:
        track_report.update(valid=False, invalid_reason=f"有效片段仅 {len(region)} 帧，短于阈值 {min_voice}")
        return {"report": track_report, "pieces": [], "cuts": [], "norm_records": []}

    # 显式全零检测：无论长度是否达到静音判定阈值，整轨全零即为无效
    if not any(v != 0 for v in region):
        track_report.update(valid=False, invalid_reason="整轨为全静音（全部样本为零）")
        return {"report": track_report, "pieces": [], "cuts": [], "norm_records": []}

    pieces, cuts = compress_silence(region, threshold, min_silence, pause_len)
    speech_pieces = [p for p in pieces if p["kind"] == "speech"]
    if not speech_pieces:
        track_report.update(valid=False, invalid_reason="整轨为全静音（无超过阈值的样本）")
        return {"report": track_report, "pieces": [], "cuts": cuts, "norm_records": []}

    norm_records = []
    for idx, piece in enumerate(pieces):
        if piece["kind"] != "speech":
            continue
        out, peak_before, peak_after, gain = normalize_samples(piece["samples"], target_peak)
        piece["samples"] = out
        norm_records.append({
            "track": label,
            "piece_index": idx,
            "input_start": piece["in_start"],
            "input_end": piece["in_end"],
            "peak_before": peak_before,
            "peak_after": peak_after,
            "gain": round(gain, 6),
        })
    track_report["voice_piece_count"] = len(speech_pieces)
    return {"report": track_report, "pieces": pieces, "cuts": cuts,
            "norm_records": norm_records}


# ----------------------------- 时间线拼接 -----------------------------

def build_timeline(track_a, track_b, place_b, pause_len_hint: int):
    """把两轨 pieces 放到成品时间线，饱和限幅混合，划分 segments/skipped。"""
    a_valid = track_a is not None and track_a["report"]["valid"]
    b_valid = track_b is not None and track_b["report"]["valid"]
    if not a_valid and not b_valid:
        raise MixerError("两轨均无有效语音，无法生成成品")

    # 计算放置位置与总长度
    a_len = sum(len(p["samples"]) for p in track_a["pieces"]) if a_valid else 0
    if b_valid:
        if place_b is None:
            b_offset = a_len if a_valid else 0
        else:
            b_offset = max(0, place_b)
    else:
        b_offset = 0
    b_len = sum(len(p["samples"]) for p in track_b["pieces"]) if b_valid else 0
    total = max(a_len, b_offset + b_len)
    if total == 0:
        raise MixerError("时间线长度为 0，无法生成成品")

    buffer = array.array("h", [0]) * total
    owner = bytearray(total)          # 位掩码：1=A, 2=B, 3=A+B
    pause_mask = bytearray(total)     # 1=该位置属于压缩停顿
    clipping_events = []

    def lay(pieces, offset, bit):
        cursor = offset
        for piece in pieces:
            data = piece["samples"]
            for j, v in enumerate(data):
                pos = cursor + j
                old = buffer[pos]
                merged = _saturate(old + v)
                if merged != old + v and len(clipping_events) < 200:
                    clipping_events.append({
                        "position": pos,
                        "sum_before_clipping": old + v,
                        "clipped_value": merged,
                    })
                buffer[pos] = merged
                owner[pos] |= bit
                if piece["kind"] == "pause":
                    pause_mask[pos] = 1
            cursor += len(data)

    if a_valid:
        track_a["report"]["output_offset"] = 0
        lay(track_a["pieces"], 0, 0x1)
    if b_valid:
        track_b["report"]["output_offset"] = b_offset
        lay(track_b["pieces"], b_offset, 0x2)

    # 按相同归属连续分块，天然严格升序
    source_name = {1: "A", 2: "B", 3: "A+B"}
    segments = []
    skipped_segments = []
    i = 0
    while i < total:
        j = i + 1
        while j < total and owner[j] == owner[i] and pause_mask[j] == pause_mask[i]:
            j += 1
        entry = {
            "start": i,
            "end": j,
            "source": None if owner[i] == 0 else source_name[owner[i]],
            "label": None,
            "action": None,
            "reason": None,
        }
        if pause_mask[i] or owner[i] == 0:
            # 压缩停顿（仍归属原轨）或两轨之间未覆盖的空白，统一计入跳过
            entry["action"] = "跳过"
            entry["reason"] = "压缩停顿" if pause_mask[i] else "轨道间空白间隙"
            skipped_segments.append(entry)
        else:
            entry["action"] = "合并"
            segments.append(entry)
        i = j

    return {
        "samples": buffer,
        "segments": segments,
        "skipped_segments": skipped_segments,
        "clipping_events": clipping_events,
        "total": total,
        "b_offset": b_offset,
    }


def attach_markers(segments, markers, skipped_rows, skipped_segments):
    """把标记匹配到 segment（中点落在标记区间且来源轨兼容），未匹配标签为 null。"""
    used_markers = set()
    for seg in segments:
        mid = (seg["start"] + seg["end"]) // 2
        for idx, m in enumerate(markers):
            if idx in used_markers:
                continue
            if not (m["start"] <= mid < m["end"]):
                continue
            # A+B 段可接受 A 或 B 的标记
            if m["source"] not in seg["source"].split("+"):
                continue
            seg["label"] = m["label"]
            used_markers.add(idx)
            break

    skipped = list(skipped_segments)
    for row in skipped_rows:
        skipped.append({
            "start": None,
            "end": None,
            "source": None,
            "label": None,
            "action": "跳过",
            "reason": f"标记文件第 {row['line_no']} 行: {row['reason']} | 原文: {row['raw']}",
        })
    return skipped


# ----------------------------- 主流程 -----------------------------

def run_pipeline(args) -> dict:
    if not args.track_a and not args.track_b:
        raise MixerError("至少需要提供 --track-a 或 --track-b 中的一轨")

    wav_paths = [p for p in (args.track_a, args.track_b) if p]
    # 两轨取较高采样率，且不低于显式/默认目标采样率（单轨场景由此保证 44100）
    target_rate = max([args.target_rate] + [read_wav(p)["sample_rate"] for p in wav_paths])

    track_a = process_track(
        "A", args.track_a, args.start_a, args.end_a, target_rate,
        args.silence_threshold, args.min_silence, args.pause_samples,
        args.target_peak, args.min_voice,
    ) if args.track_a else None
    track_b = process_track(
        "B", args.track_b, args.start_b, args.end_b, target_rate,
        args.silence_threshold, args.min_silence, args.pause_samples,
        args.target_peak, args.min_voice,
    ) if args.track_b else None

    timeline = build_timeline(track_a, track_b, args.place_b, args.pause_samples)

    skipped_rows = []
    markers = []
    if args.markers:
        markers, skipped_rows = parse_markers(args.markers, timeline["total"])
    skipped = attach_markers(timeline["segments"], markers, skipped_rows,
                             timeline["skipped_segments"])

    # 组装报告
    tracks = {}
    silence_cuts = []
    normalization = []
    for tk in (track_a, track_b):
        if tk is None:
            continue
        tracks[tk["report"]["track"]] = tk["report"]
        for cut in tk["cuts"]:
            silence_cuts.append({"track": tk["report"]["track"], **cut})
        normalization.extend(tk["norm_records"])

    # 排序自检：segments 必须严格升序
    starts = [s["start"] for s in timeline["segments"]]
    if starts != sorted(starts) or len(set(starts)) != len(starts):
        raise MixerError("内部错误：segments 未满足严格升序")

    report = {
        "output_wav": os.path.abspath(args.output),
        "sample_rate": target_rate,
        "channels": 1,
        "sample_width_bytes": 2,
        "total_frames": timeline["total"],
        "duration_seconds": round(timeline["total"] / target_rate, 6),
        "target_peak": args.target_peak,
        "silence_threshold": args.silence_threshold,
        "min_silence_samples": args.min_silence,
        "pause_samples": args.pause_samples,
        "tracks": tracks,
        "segments": timeline["segments"],
        "skipped": skipped,
        "silence_cuts": silence_cuts,
        "normalization": normalization,
        "clipping": {
            "event_count": len(timeline["clipping_events"]),
            "events": timeline["clipping_events"],
        },
    }

    out_wav = os.path.abspath(args.output)
    out_json = os.path.splitext(out_wav)[0] + ".json"
    out_dir = os.path.dirname(out_wav) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise MixerError(f"无法创建输出目录 {out_dir}: {exc}") from exc

    # 覆盖保护：在写任何临时文件之前检查
    if not args.overwrite and (os.path.exists(out_wav) or os.path.exists(out_json)):
        existed = out_wav if os.path.exists(out_wav) else out_json
        raise MixerError(
            f"输出文件已存在: {existed}；默认拒绝覆盖，请加 --overwrite 显式确认"
        )

    tmp_files = []
    try:
        wav_bytes = build_wav_bytes(timeline["samples"], target_rate)
        fd, tmp_wav = tempfile.mkstemp(prefix=".tmp_podmix_", suffix=".wav", dir=out_dir)
        os.close(fd)
        tmp_files.append(tmp_wav)
        with open(tmp_wav, "wb") as f:
            f.write(wav_bytes)
        # 写出后立即读回校验
        verify_wav(tmp_wav, timeline["total"], target_rate)

        fd, tmp_json = tempfile.mkstemp(prefix=".tmp_podmix_", suffix=".json", dir=out_dir)
        os.close(fd)
        tmp_files.append(tmp_json)
        with open(tmp_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        os.replace(tmp_wav, out_wav)
        os.replace(tmp_json, out_json)
    except BaseException:
        for tmp in tmp_files:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        raise
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="双人播客单声道 16bit WAV 后期拼接工具（标准库实现）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--track-a", help="主播 A 的 WAV 路径（A、B 至少提供一轨）")
    p.add_argument("--track-b", help="主播 B 的 WAV 路径")
    p.add_argument("--start-a", type=int, help="A 轨起始偏移（目标采样率采样）")
    p.add_argument("--end-a", type=int, help="A 轨结束偏移，省略表示到文件末尾")
    p.add_argument("--start-b", type=int, help="B 轨起始偏移")
    p.add_argument("--end-b", type=int, help="B 轨结束偏移")
    p.add_argument("--place-b", type=int,
                   help="B 轨在成品时间线上的起始位置，默认紧接 A 轨之后")
    p.add_argument("--markers", help="标记文件路径，省略则全部标签为 null")
    p.add_argument("--output", required=True, help="成品 WAV 输出路径（JSON 同名 .json）")
    p.add_argument("--target-rate", type=int, default=DEFAULT_TARGET_RATE,
                   help="目标采样率下限；两轨不一致时实际取较高者")
    p.add_argument("--overwrite", action="store_true", help="允许覆盖已存在的同名输出")
    p.add_argument("--target-peak", type=int, default=TARGET_PEAK_DEFAULT, help="归一化目标峰值")
    p.add_argument("--silence-threshold", type=int, default=SILENCE_THRESHOLD_DEFAULT,
                   help="|样本| <= 该值判定为静音")
    p.add_argument("--min-silence", type=int, default=MIN_SILENCE_DEFAULT,
                   help="触发压缩的最短连续静音采样数")
    p.add_argument("--pause-samples", type=int, default=PAUSE_SAMPLES_DEFAULT,
                   help="长静音压缩后保留的短停顿采样数")
    p.add_argument("--min-voice", type=int, default=MIN_VOICE_FRAMES_DEFAULT,
                   help="判定极短片段的最小帧数")
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_arg_parser().parse_args(argv)
    try:
        report = run_pipeline(args)
    except MixerError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # 任何未预期失败也保证非零退出且不留半成品
        print(f"错误：处理失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"处理完成：{report['output_wav']}")
    print(f"  目标采样率={report['sample_rate']} Hz, 总帧数={report['total_frames']}, "
          f"时长={report['duration_seconds']}s")
    print(f"  合并段={len(report['segments'])} 段, 跳过记录={len(report['skipped'])} 条, "
          f"限幅事件={report['clipping']['event_count']} 次")
    return 0


if __name__ == "__main__":
    sys.exit(main())
