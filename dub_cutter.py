# -*- coding: utf-8 -*-
"""
dub_cutter.py —— 本地化配音小样批量剪辑工具（仅依赖 Python 3.10+ 标准库）

功能：
  读取 edit_list.json 与单声道 16bit PCM WAV 录音，按清单裁出多段台词，
  逐段按 target_dbfs 做峰值归一化（越限限幅），输出统一 16bit 单声道 WAV，
  并生成 report.json。

校验分两层：
  1. 清单级致命错误（结构错误、起止倒置/非非负整数、终点超出录音总时长等）：
     整体拒绝执行，退出码 2，不写任何片段 WAV，也不写 report.json。
  2. 条目级错误（id 重复、id 含 Windows 文件名非法字符等）：仅拒绝该条目，
     其余条目正常产出，报告中记录原因。

用法：
  python dub_cutter.py [--wav PATH] [--edit-list PATH]
                       [--output-dir DIR] [--report PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import sys
import tempfile
import wave

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

SAMPLE_RATE = 44100          # 任务样本采样率
SAMPLE_WIDTH = 2             # 16bit
N_CHANNELS = 1               # 单声道
INT16_MIN = -32768
INT16_MAX = 32767
INT16_FULL_SCALE = 32767.0   # 0 dBFS 参考满幅

# Windows 文件名非法字符
_WINDOWS_BAD_CHARS = set('<>:"/\\|?*')
# Windows 保留设备名（不区分大小写）
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

STATUS_SUCCESS = "success"   # 成功写盘
STATUS_REJECTED = "rejected"  # 清单校验阶段被拒绝
STATUS_FAILED = "failed"     # 写盘阶段失败


class FatalListError(Exception):
    """清单级致命错误：必须整体拒绝、退出码非零、不产出任何文件。"""


# ----------------------------------------------------------------------
# 基础校验工具
# ----------------------------------------------------------------------

def _is_real_int(value) -> bool:
    """非负/普通整数判断；bool 是 int 的子类，显式排除。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value) -> bool:
    """有限数值判断（排除 bool、NaN、Inf）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def validate_windows_filename(seg_id: str) -> str | None:
    """
    校验片段 id 作为 Windows 文件名（不含扩展名）是否合法。
    合法返回 None；非法返回简短中文原因。
    """
    if not isinstance(seg_id, str) or seg_id == "":
        return "id 必须为非空字符串"
    if any(ord(ch) < 32 for ch in seg_id):
        return "id 含控制字符，不能作为文件名"
    bad = sorted(set(seg_id) & _WINDOWS_BAD_CHARS)
    if bad:
        return "id 含 Windows 文件名非法字符: " + " ".join(bad)
    if seg_id in (".", ".."):
        return "id 不能为 . 或 .."
    if seg_id.endswith(" ") or seg_id.endswith("."):
        return "id 不能以空格或句点结尾"
    stem = seg_id.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED:
        return f"id 使用了 Windows 保留设备名: {stem}"
    if len(seg_id) > 120:
        return "id 过长（超过 120 字符）"
    return None


def ms_to_frame(ms: int, sample_rate: int) -> int:
    """毫秒位置按 乘采样率整除 1000 换算帧。"""
    return ms * sample_rate // 1000


# ----------------------------------------------------------------------
# 清单级结构校验（任何一条失败 -> 整体拒绝）
# ----------------------------------------------------------------------

def load_and_validate_structure(edit_list_path: str, total_frames: int,
                                sample_rate: int) -> list[dict]:
    """
    读取并做清单级结构校验，返回原始条目列表（顺序与文件一致）。
    抛出 FatalListError 表示整体非法，调用方不得产出任何文件。
    """
    try:
        with open(edit_list_path, "r", encoding="utf-8-sig") as fp:
            data = json.load(fp)
    except FileNotFoundError:
        raise FatalListError(f"找不到编辑清单: {edit_list_path}")
    except json.JSONDecodeError as exc:
        raise FatalListError(f"edit_list.json 不是合法 JSON: {exc}")
    except OSError as exc:
        raise FatalListError(f"读取编辑清单失败: {exc}")

    if not isinstance(data, list):
        raise FatalListError("编辑清单顶层必须是数组（每条一个片段对象）")
    if len(data) == 0:
        raise FatalListError("编辑清单为空，至少需要一个片段条目")

    for idx, item in enumerate(data):
        label = f"第 {idx + 1} 条"
        if not isinstance(item, dict):
            raise FatalListError(f"{label} 条目必须是 JSON 对象")

        for key in ("id", "start_ms", "end_ms", "target_dbfs"):
            if key not in item:
                raise FatalListError(f"{label} 缺少字段: {key}")

        start_ms = item["start_ms"]
        end_ms = item["end_ms"]
        if not _is_real_int(start_ms) or start_ms < 0:
            raise FatalListError(
                f"{label}(id={item.get('id')!r}) start_ms 必须为非负整数")
        if not _is_real_int(end_ms) or end_ms < 0:
            raise FatalListError(
                f"{label}(id={item.get('id')!r}) end_ms 必须为非负整数")
        if start_ms >= end_ms:
            raise FatalListError(
                f"{label}(id={item.get('id')!r}) 起点必须严格早于终点: "
                f"start_ms={start_ms}, end_ms={end_ms}")

        if not _is_finite_number(item["target_dbfs"]):
            raise FatalListError(
                f"{label}(id={item.get('id')!r}) target_dbfs 必须为有限数值")

        # 终点超出录音总时长 -> 清单整体非法
        end_frame = ms_to_frame(end_ms, sample_rate)
        if end_frame > total_frames:
            raise FatalListError(
                f"{label}(id={item.get('id')!r}) 终点 {end_ms}ms 换算 "
                f"{end_frame} 帧，超出录音总时长 {total_frames} 帧，"
                "清单整体非法，拒绝执行")

    return data


# ----------------------------------------------------------------------
# 条目级校验（id 文件名合法性 / 重复 id）
# ----------------------------------------------------------------------

def build_plan(raw_items: list[dict], sample_rate: int) -> list[dict]:
    """
    在结构校验通过后做条目级校验，生成执行计划。
    每个计划条目带 status: ok / rejected 与 reason。
    重复 id：所有同名条目一律拒绝，保证只有唯一合法 id 能产出 WAV。
    """
    id_counts: dict[str, int] = {}
    for item in raw_items:
        seg_id = item["id"]
        if isinstance(seg_id, str):
            id_counts[seg_id] = id_counts.get(seg_id, 0) + 1

    plan: list[dict] = []
    for item in raw_items:
        seg_id = item["id"]
        start_frame = ms_to_frame(item["start_ms"], sample_rate)
        end_frame = ms_to_frame(item["end_ms"], sample_rate)
        frame_count = end_frame - start_frame

        entry = {
            "id": seg_id,
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "target_dbfs": float(item["target_dbfs"]),
            "start_frame": start_frame,
            "end_frame": end_frame,
            "frame_count": frame_count,
            "status": "ok",
            "reason": None,
        }

        # 结构校验已保证字段齐全；这里只处理文件名层面的 id 问题
        reason = validate_windows_filename(seg_id) if isinstance(seg_id, str) \
            else "id 必须为非空字符串"
        if reason is None and id_counts.get(seg_id, 0) > 1:
            reason = f"id 重复（清单中出现 {id_counts[seg_id]} 次），拒绝所有同名条目"
        if reason is not None:
            entry["status"] = STATUS_REJECTED
            entry["reason"] = reason
        plan.append(entry)
    return plan


# ----------------------------------------------------------------------
# 音频处理
# ----------------------------------------------------------------------

def read_wav_mono16(wav_path: str) -> tuple[array.array, int]:
    """
    读取单声道 16bit PCM WAV，返回 (有符号 16bit 样本数组, 采样率)。
    非 PCM / 非单声道 / 非 16bit 直接视为致命输入错误。
    """
    try:
        with wave.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            comp_type = wf.getcomptype()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except FileNotFoundError:
        raise FatalListError(f"找不到录音文件: {wav_path}")
    except wave.Error as exc:
        raise FatalListError(f"无法解析 WAV 文件: {exc}")
    except OSError as exc:
        raise FatalListError(f"读取 WAV 文件失败: {exc}")

    if comp_type != "NONE":
        raise FatalListError(f"仅支持 PCM 未压缩 WAV，当前压缩类型: {comp_type}")
    if n_channels != N_CHANNELS:
        raise FatalListError(f"仅支持单声道 WAV，当前声道数: {n_channels}")
    if sample_width != SAMPLE_WIDTH:
        raise FatalListError(f"仅支持 16bit WAV，当前采样宽度: {sample_width} 字节")

    samples = array.array("h")
    samples.frombytes(raw)
    return samples, sample_rate


def normalize_clip(samples: array.array, target_dbfs: float) -> array.array:
    """
    按目标峰值 dBFS 做峰值归一化；增益后越界样本做硬限幅，
    保证输出峰值不超过 16bit 满幅 [-32768, 32767]。
    """
    out = array.array("h", b"\x00\x00" * len(samples))
    if len(samples) == 0:
        return out

    peak = max(abs(int(s)) for s in samples)
    if peak <= 0:
        # 全静音片段没有可归一化的峰值，原样输出（仍为全 0）
        out[:] = array.array("h", samples)
        return out

    target_peak = INT16_FULL_SCALE * (10.0 ** (target_dbfs / 20.0))
    gain = target_peak / float(peak)

    for i, s in enumerate(samples):
        scaled = int(round(float(s) * gain))
        if scaled > INT16_MAX:
            scaled = INT16_MAX
        elif scaled < INT16_MIN:
            scaled = INT16_MIN
        out[i] = scaled
    return out


def write_wav_atomic(path: str, samples: array.array, sample_rate: int) -> None:
    """
    原子写 WAV：先写同目录临时文件，成功后再替换目标文件；
    写盘失败会清理临时文件并抛出异常，不会留下半截 WAV 污染成功数据。
    """
    out_dir = os.path.dirname(os.path.abspath(path))
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp.wav", dir=out_dir)
    os.close(tmp_fd)
    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(N_CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())
        os.replace(tmp_path, path)
    except BaseException:
        # 任何失败都要回收临时文件，避免污染输出目录
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按 edit_list.json 从单声道 16bit PCM WAV 中批量裁出"
                    "归一化配音小样，并生成 report.json")
    parser.add_argument("--wav", default="sample.wav",
                        help="源录音 WAV 路径（默认 sample.wav）")
    parser.add_argument("--edit-list", default="edit_list.json",
                        help="编辑清单 JSON 路径（默认 edit_list.json）")
    parser.add_argument("--output-dir", default="output",
                        help="片段 WAV 输出目录（默认 output）")
    parser.add_argument("--report", default=None,
                        help="report.json 路径（默认 <输出目录>/report.json）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只校验清单并按清单顺序打印将产出的片段名，"
                             "不创建目录、不写音频与报告")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report_path = args.report or os.path.join(args.output_dir, "report.json")

    # 1) 读源录音（先于任何输出动作）
    source_samples, sample_rate = read_wav_mono16(args.wav)
    total_frames = len(source_samples)

    # 2) 清单级结构校验（含终点越界）。失败即整体拒绝，无任何落盘动作。
    raw_items = load_and_validate_structure(
        args.edit_list, total_frames, sample_rate)

    # 3) 条目级校验（非法文件名 / 重复 id）
    plan = build_plan(raw_items, sample_rate)

    # 4) dry-run：只打印，不建目录、不写任何文件
    if args.dry_run:
        print("=== dry-run：清单校验完成，不写任何文件 ===")
        for entry in plan:
            if entry["status"] == "ok":
                print(f"[将产出] {entry['id']}.wav "
                      f"({entry['frame_count']} 帧, "
                      f"target_dbfs={entry['target_dbfs']:g})")
            else:
                print(f"[已拒绝] {entry['id']!r} 原因: {entry['reason']}")
        rejected = sum(1 for e in plan if e["status"] == STATUS_REJECTED)
        print(f"=== 合计 {len(plan)} 条，将产出 {len(plan) - rejected} 个片段，"
              f"拒绝 {rejected} 条 ===")
        return 0

    # 5) 正式执行：此时才创建输出目录
    try:
        os.makedirs(args.output_dir, exist_ok=True)
    except OSError as exc:
        print(f"错误: 无法创建输出目录 {args.output_dir}: {exc}",
              file=sys.stderr)
        return 2

    report_entries: list[dict] = []
    write_failures = 0
    rejected_count = 0

    for entry in plan:
        record = {
            "id": entry["id"],
            "status": entry["status"],
            "start_ms": entry["start_ms"],
            "end_ms": entry["end_ms"],
            "target_dbfs": entry["target_dbfs"],
            "start_frame": entry["start_frame"],
            "end_frame": entry["end_frame"],
            "output_frames": 0,
            "output_file": None,
            "reason": entry["reason"],
        }

        if entry["status"] == STATUS_REJECTED:
            rejected_count += 1
            report_entries.append(record)
            print(f"[拒绝] id={entry['id']!r}: {entry['reason']}")
            continue

        out_name = f"{entry['id']}.wav"
        out_path = os.path.join(args.output_dir, out_name)
        try:
            clip = source_samples[entry["start_frame"]:entry["end_frame"]]
            if len(clip) != entry["frame_count"]:
                raise RuntimeError(
                    f"切片帧数异常: 期望 {entry['frame_count']}，实际 {len(clip)}")
            processed = normalize_clip(clip, entry["target_dbfs"])
            write_wav_atomic(out_path, processed, sample_rate)
        except Exception as exc:  # 单个片段写盘失败不影响其余片段
            write_failures += 1
            record["status"] = STATUS_FAILED
            record["reason"] = f"写盘失败: {type(exc).__name__}: {exc}"
            report_entries.append(record)
            print(f"[失败] id={entry['id']!r}: {record['reason']}",
                  file=sys.stderr)
            continue

        record["status"] = STATUS_SUCCESS
        record["output_frames"] = entry["frame_count"]
        record["output_file"] = out_name
        record["reason"] = None
        report_entries.append(record)
        print(f"[完成] {out_name} ({entry['frame_count']} 帧)")

    success_count = sum(
        1 for r in report_entries if r["status"] == STATUS_SUCCESS)

    report = {
        "source_wav": os.path.basename(args.wav),
        "sample_rate": sample_rate,
        "channels": N_CHANNELS,
        "sample_width_bytes": SAMPLE_WIDTH,
        "total_source_frames": total_frames,
        "entry_count": len(report_entries),
        "success_count": success_count,
        "rejected_count": rejected_count,
        "failed_count": write_failures,
        # 整体完成情况：rejected 是清单主动拒绝，不算“未完成”；
        # 只有写盘失败才算未完成
        "all_writes_succeeded": write_failures == 0,
        "all_entries_exported": (
            write_failures == 0 and rejected_count == 0),
        "entries": report_entries,  # 顺序与清单完全一致
    }

    try:
        with open(report_path, "w", encoding="utf-8") as fp:
            json.dump(report, fp, ensure_ascii=False, indent=2)
        print(f"[报告] 已写入 {report_path}")
    except OSError as exc:
        print(f"错误: report.json 写入失败: {exc}", file=sys.stderr)
        return 2

    print(f"=== 成功 {success_count}，拒绝 {rejected_count}，"
          f"写盘失败 {write_failures} ===")
    # 存在被拒绝条目或写盘失败时退出码为 1，全部成功为 0
    return 1 if (write_failures > 0 or rejected_count > 0) else 0


def main() -> None:
    try:
        sys.exit(run())
    except FatalListError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
