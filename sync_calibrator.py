# -*- coding: utf-8 -*-
"""游戏技能声画同步校准工具（Windows 离线运行）。

读取校准清单 JSON，逐个技能：
  1. 解析音效 WAV（容错处理文件头声明长度与实际大小不符的情况），
     以第一个非静音采样点作为实际起音时刻；
  2. 读取关键帧时序 JSON（标称起音毫秒 + 关键帧坐标）；
  3. 计算同步偏差（标称起音 - 实际起音，四舍五入到整数毫秒）；
  4. 为合法关键帧生成 320x180 预览帧 PNG（叠加该帧对应音效时间位置）。

输出（均位于清单指定的输出目录下）：
  - <技能名>_<帧序号>.png   预览帧
  - sync_deviation_report.json  同步偏差报告
  - master_report.json          总报告（按清单顺序列出各技能状态）

用法：
  python sync_calibrator.py <校准清单.json>
"""

import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

FRAME_WIDTH = 320
FRAME_HEIGHT = 180
DEFAULT_FPS = 60.0

STATUS_COMPLETE = "完整"
STATUS_UNAVAILABLE = "不可用"
STATUS_TIMING_MISSING = "时序缺失"
STATUS_PARTIAL = "部分完成"
STATUS_INVALID_ENTRY = "清单条目无效"


# ---------------------------------------------------------------------------
# WAV 解析（手写 RIFF 解析，容忍文件头声明长度与实际文件大小不符）
# ---------------------------------------------------------------------------

class WavError(Exception):
    """WAV 无法读取时抛出。"""


def read_wav(path):
    """读取 WAV 文件，返回 (sample_rate, samples[np.int16 一维])。

    - 以实际可读采样数为准：data 块声明长度超出文件实际大小时截断读取；
    - 采样率为 0 时抛出 WavError（采样率不合法）。
    """
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise WavError(f"无法读取文件: {exc}") from exc

    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise WavError("不是有效的 RIFF/WAVE 文件")

    pos = 12
    fmt = None
    data = None
    data_declared = 0
    # 逐个 chunk 扫描；chunk 声明长度越界时按实际剩余字节截断，不崩溃。
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", raw, pos + 4)[0]
        body_start = pos + 8
        body = raw[body_start:body_start + chunk_size]  # 越界自动截断
        if chunk_id == b"fmt ":
            if len(body) < 16:
                raise WavError("fmt 块长度不足")
            fmt = body
        elif chunk_id == b"data":
            data = body
            data_declared = chunk_size
        # 移动到下一 chunk（按声明长度 + 对齐字节），但防止死循环
        step = 8 + chunk_size + (chunk_size & 1)
        if step <= 0:
            break
        pos += step

    if fmt is None:
        raise WavError("缺少 fmt 块")
    if data is None:
        raise WavError("缺少 data 块")

    audio_format, channels, sample_rate, _byte_rate, block_align, bits = \
        struct.unpack_from("<HHIIHH", fmt, 0)

    if sample_rate <= 0:
        raise WavError(f"采样率不合法: {sample_rate}")
    if channels <= 0:
        raise WavError(f"声道数不合法: {channels}")
    if audio_format != 1:
        raise WavError(f"仅支持 PCM 格式 (audio_format={audio_format})")
    if bits not in (8, 16):
        raise WavError(f"仅支持 8/16 位采样 (bits={bits})")

    # 以实际可读字节数为准（声明长度可能与文件实际大小不符）
    usable = min(len(data), data_declared) if data_declared else len(data)
    data = data[:usable]

    if bits == 16:
        usable -= usable % 2
        arr = np.frombuffer(data[:usable], dtype="<i2").astype(np.int32)
    else:  # 8-bit PCM 无符号，转为有符号
        arr = np.frombuffer(data, dtype=np.uint8).astype(np.int32) - 128

    if block_align and channels > 1:
        frames = len(arr) // channels
        arr = arr[:frames * channels].reshape(frames, channels)
        # 任一声道非静音即视为非静音
        arr = np.abs(arr).max(axis=1)
    else:
        arr = np.abs(arr)

    return sample_rate, arr


def find_actual_onset_ms(sample_rate, abs_samples):
    """第一个非静音采样点对应的毫秒时刻；全静音返回 None。"""
    nonzero = np.nonzero(abs_samples > 0)[0]
    if nonzero.size == 0:
        return None
    return nonzero[0] * 1000.0 / sample_rate


# ---------------------------------------------------------------------------
# 偏差计算
# ---------------------------------------------------------------------------

def round_half_away(value):
    """四舍五入到整数（0.5 远离零）。绝对值小于 1 的结果自然为 0。"""
    if value >= 0:
        return int(math.floor(value + 0.5))
    return -int(math.floor(-value + 0.5))


# ---------------------------------------------------------------------------
# 预览帧渲染
# ---------------------------------------------------------------------------

def render_frame(path, skill_name, frame_index, time_ms, point):
    """渲染 320x180 预览帧：深色背景 + 关键帧标记 + 时间位置文字。"""
    img = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), (24, 26, 32))
    draw = ImageDraw.Draw(img)
    x, y = point
    r = 8
    draw.ellipse((x - r, y - r, x + r, y + r), outline=(255, 200, 60), width=3)
    draw.line((x - r - 4, y, x + r + 4, y), fill=(255, 200, 60), width=1)
    draw.line((x, y - r - 4, x, y + r + 4), fill=(255, 200, 60), width=1)
    draw.text((8, 6), f"{skill_name} frame={frame_index}", fill=(220, 220, 220))
    draw.text((8, 22), f"t={time_ms:.1f}ms", fill=(120, 220, 120))
    img.save(path)


# ---------------------------------------------------------------------------
# 单技能处理
# ---------------------------------------------------------------------------

def process_skill(entry, index, output_dir, standard_rate):
    """处理单个技能条目，返回报告字典。任何失败都不向上抛出。"""
    name = entry.get("name") or f"skill_{index}"
    record = {
        "name": name,
        "status": None,
        "nominal_onset_ms": entry.get("nominal_onset_ms"),
        "actual_onset_ms": None,
        "deviation_ms": None,
        "sample_rate": None,
        "resample_required": False,
        "notes": [],
        "issues": [],
        "frames": [],
    }

    # --- 清单条目字段检查（缺字段不中止整体流程） ---
    missing = [k for k in ("wav", "timing") if not entry.get(k)]
    if "nominal_onset_ms" not in entry:
        missing.append("nominal_onset_ms")
    if missing:
        record["status"] = STATUS_INVALID_ENTRY
        record["issues"].append(f"清单条目缺少字段: {', '.join(missing)}")
        return record

    # --- 读取音效 WAV ---
    try:
        sample_rate, samples = read_wav(entry["wav"])
    except WavError as exc:
        record["status"] = STATUS_UNAVAILABLE
        record["issues"].append(f"音效不可用: {exc}")
        return record

    record["sample_rate"] = sample_rate
    if standard_rate and sample_rate != standard_rate:
        record["resample_required"] = True
        record["notes"].append(
            f"需重采样: 实际采样率 {sample_rate}Hz, 清单声明标准采样率 {standard_rate}Hz"
        )

    # --- 实际起音（按原始采样数据计算，不受采样率声明不一致影响） ---
    onset = find_actual_onset_ms(sample_rate, samples)
    if onset is None:
        record["status"] = STATUS_UNAVAILABLE
        record["issues"].append("音效不可用: 全部采样均为静音")
        return record
    record["actual_onset_ms"] = round(onset, 3)

    # --- 读取关键帧时序 JSON ---
    timing_path = Path(entry["timing"])
    timing = None
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
        if not isinstance(timing, dict) or not isinstance(timing.get("keyframes"), list):
            raise ValueError("缺少 keyframes 数组")
    except (OSError, ValueError) as exc:
        record["status"] = STATUS_TIMING_MISSING
        record["issues"].append(f"关键帧时序缺失或格式错误: {exc}")
        # 仍记录音效起音，但不生成预览帧、不计算偏差
        return record

    nominal = timing.get("nominal_onset_ms", record["nominal_onset_ms"])
    record["nominal_onset_ms"] = nominal
    if isinstance(nominal, (int, float)):
        record["deviation_ms"] = round_half_away(nominal - onset)

    # --- 生成预览帧 ---
    keyframes = []
    for kf in timing["keyframes"]:
        if isinstance(kf, dict) and isinstance(kf.get("frame"), int):
            keyframes.append(kf)
    if not keyframes:
        record["status"] = STATUS_TIMING_MISSING
        record["issues"].append("关键帧时序缺失或格式错误: 无有效关键帧")
        return record

    fps = timing.get("fps", DEFAULT_FPS)
    if not isinstance(fps, (int, float)) or fps <= 0:
        fps = DEFAULT_FPS
    max_frame = max(kf["frame"] for kf in keyframes)
    expected = max_frame + 1  # 帧数 = 最大帧序号 + 1

    out_of_bounds = 0
    for kf in sorted(keyframes, key=lambda k: k["frame"]):
        idx = kf["frame"]
        x, y = kf.get("x"), kf.get("y")
        if (not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                or not (0 <= x < FRAME_WIDTH) or not (0 <= y < FRAME_HEIGHT)):
            out_of_bounds += 1
            record["issues"].append(
                f"坐标越界: 帧 {idx} 坐标 ({x}, {y}) 超出 {FRAME_WIDTH}x{FRAME_HEIGHT}，已跳过该帧"
            )
            continue
        time_ms = idx * 1000.0 / fps
        frame_name = f"{name}_{idx}.png"
        frame_path = output_dir / frame_name
        try:
            render_frame(frame_path, name, idx, time_ms, (x, y))
        except OSError as exc:
            record["issues"].append(f"帧 {idx} 写入失败: {exc}")
            continue
        record["frames"].append(str(frame_path))

    generated = len(record["frames"])
    if generated == expected:
        record["status"] = STATUS_COMPLETE
    else:
        record["status"] = STATUS_PARTIAL
        record["notes"].append(
            f"应有 {expected} 帧（最大帧序号 {max_frame} + 1），实际生成 {generated} 帧"
        )
    return record


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv):
    if len(argv) != 2:
        print("用法: python sync_calibrator.py <校准清单.json>", file=sys.stderr)
        return 2

    manifest_path = Path(argv[1])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"错误: 无法读取校准清单 {manifest_path}: {exc}", file=sys.stderr)
        return 2

    output_dir = Path(manifest.get("output_dir", "output"))
    standard_rate = manifest.get("standard_sample_rate")

    # 输出目录不存在时自动创建；创建失败则终止处理，不生成任何报告
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        if not output_dir.is_dir():
            raise OSError("目标路径已存在且不是目录")
    except OSError as exc:
        print(f"错误: 无法创建输出目录 {output_dir}: {exc}", file=sys.stderr)
        return 1

    skills = manifest.get("skills")
    if not isinstance(skills, list):
        print("错误: 校准清单缺少 skills 数组", file=sys.stderr)
        return 2

    records = []
    for i, entry in enumerate(skills):
        if not isinstance(entry, dict):
            records.append({
                "name": f"skill_{i}", "status": STATUS_INVALID_ENTRY,
                "nominal_onset_ms": None, "actual_onset_ms": None,
                "deviation_ms": None, "sample_rate": None,
                "resample_required": False,
                "notes": [], "issues": ["清单条目不是对象"], "frames": [],
            })
            continue
        try:
            record = process_skill(entry, i, output_dir, standard_rate)
        except Exception as exc:  # 单技能失败不影响其他技能
            record = {
                "name": entry.get("name") or f"skill_{i}",
                "status": STATUS_UNAVAILABLE,
                "nominal_onset_ms": entry.get("nominal_onset_ms"),
                "actual_onset_ms": None, "deviation_ms": None,
                "sample_rate": None, "resample_required": False,
                "notes": [], "issues": [f"处理异常: {exc!r}"], "frames": [],
            }
        records.append(record)
        print(f"[{record['status']}] {record['name']}")

    # --- 同步偏差报告 ---
    deviation_report = {
        "manifest": str(manifest_path),
        "standard_sample_rate": standard_rate,
        "skills": [
            {
                "name": r["name"],
                "status": r["status"],
                "nominal_onset_ms": r["nominal_onset_ms"],
                "actual_onset_ms": r["actual_onset_ms"],
                "deviation_ms": r["deviation_ms"],
                "sample_rate": r["sample_rate"],
                "resample_required": r["resample_required"],
                "notes": r["notes"],
            }
            for r in records
        ],
    }
    deviation_path = output_dir / "sync_deviation_report.json"
    deviation_path.write_text(
        json.dumps(deviation_report, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 总报告（按清单技能顺序） ---
    master_report = {
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "summary": {
            "total": len(records),
            STATUS_COMPLETE: sum(1 for r in records if r["status"] == STATUS_COMPLETE),
            STATUS_PARTIAL: sum(1 for r in records if r["status"] == STATUS_PARTIAL),
            STATUS_UNAVAILABLE: sum(1 for r in records if r["status"] == STATUS_UNAVAILABLE),
            STATUS_TIMING_MISSING: sum(1 for r in records if r["status"] == STATUS_TIMING_MISSING),
            STATUS_INVALID_ENTRY: sum(1 for r in records if r["status"] == STATUS_INVALID_ENTRY),
        },
        "skills": records,
    }
    master_path = output_dir / "master_report.json"
    master_path.write_text(
        json.dumps(master_report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n同步偏差报告: {deviation_path}")
    print(f"总报告:       {master_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
