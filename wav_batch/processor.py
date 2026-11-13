# -*- coding: utf-8 -*-
"""清单驱动的 WAV 批量裁剪与 RMS 增益对齐处理器（仅标准库）。"""
from __future__ import annotations

import csv
import json
import os

from wav_tools import WavError, apply_gain, read_wav, rms_dbfs, slice_samples, write_wav

# 清单表头（顺序固定）
HEADER = ["id", "source", "start", "end", "target_dbfs", "output"]
# 目标响度合法范围（dBFS）
TARGET_MIN, TARGET_MAX = -40.0, 0.0
# 裁剪时长允许误差（秒），对应 1 毫秒验收要求
DURATION_TOLERANCE = 0.001


def _abs(base_dir: str, path: str) -> str:
    """清单中的相对路径相对清单所在目录解析，返回绝对路径。"""
    if os.path.isabs(path):
        return os.path.normpath(path)
    return os.path.normpath(os.path.join(base_dir, path))


def _load_rows(manifest_path: str) -> list[dict[str, str]]:
    """读取 CSV 清单并做全局校验。

    全局失败（文件缺失/不可读/无有效数据行）抛出对应中文异常；
    正常时返回去掉空行后的原始字符串行列表（dict 形式）。
    """
    if not os.path.exists(manifest_path):
        raise FileNotFoundError("清单文件不存在：%s" % manifest_path)
    if not os.path.isfile(manifest_path):
        raise OSError("清单路径不是普通文件，无法读取：%s" % manifest_path)

    try:
        # utf-8-sig 可同时兼容带/不带 BOM 的清单
        with open(manifest_path, "r", encoding="utf-8-sig", newline="") as f:
            content = f.read()
    except OSError as exc:
        raise OSError("清单文件无法读取：%s（%s）" % (manifest_path, exc)) from exc

    if not content.strip():
        raise ValueError("清单为空：文件中没有任何内容")

    rows = list(csv.DictReader(content.splitlines()))
    if not rows:
        raise ValueError("清单为空：只有表头或空白，没有任何有效数据行")

    missing_cols = [c for c in HEADER if c not in rows[0] or rows[0].get(c) is None]
    if missing_cols:
        raise ValueError("清单表头缺少必需列：%s" % "、".join(missing_cols))

    # 丢弃完全空白的行（每个字段都为空）
    cleaned = [r for r in rows if any((r.get(c) or "").strip() for c in HEADER)]
    if not cleaned:
        raise ValueError("清单为空：表头之外没有任何有效数据行")
    return cleaned


def _fail(entry_id: str, reason: str) -> dict:
    """构造一条失败报告条目。"""
    return {
        "id": entry_id,
        "status": "失败",
        "output_path": None,
        "error": reason,
        "warnings": [],
    }


def _process_row(row: dict[str, str], base_dir: str, out_dir: str) -> dict:
    """处理清单中的一行；任何异常都转换为该条目的失败，不影响其他行。"""
    entry_id = (row.get("id") or "").strip() or "(未命名条目)"

    # 1. 字段完整性
    raw = {c: (row.get(c) or "").strip() for c in HEADER}
    empty_fields = [c for c in ("source", "start", "end", "target_dbfs", "output") if not raw[c]]
    if empty_fields:
        return _fail(entry_id, "清单字段缺失或为空：%s" % "、".join(empty_fields))

    # 2. 数值解析（时间保留三位小数语义，这里允许任意精度的合法小数）
    try:
        start = float(raw["start"])
        end = float(raw["end"])
    except ValueError:
        return _fail(
            entry_id,
            "时间参数无法解析：起始=%r、结束=%r，须为以秒为单位的数字"
            % (raw["start"], raw["end"]),
        )
    try:
        target_dbfs = float(raw["target_dbfs"])
    except ValueError:
        return _fail(entry_id, "目标响度无法解析：%r，须为 -40 至 0 之间的数字（dBFS）" % raw["target_dbfs"])

    if start < 0 or end < 0:
        return _fail(entry_id, "时间参数不能为负数：起始=%.3f、结束=%.3f" % (start, end))
    if not (end > start):
        return _fail(
            entry_id,
            "时间区间非法：结束 %.3f 必须大于起始 %.3f" % (end, start),
        )
    if not (TARGET_MIN <= target_dbfs <= TARGET_MAX):
        return _fail(
            entry_id,
            "目标响度 %.3f dBFS 超出允许范围（%d 至 %d dBFS）"
            % (target_dbfs, TARGET_MIN, TARGET_MAX),
        )

    # 3. 路径解析与"输入输出相同"保护（在文件存在性检查之前）
    source_path = _abs(base_dir, raw["source"])
    output_path = _abs(out_dir, raw["output"])
    if os.path.normcase(os.path.abspath(source_path)) == os.path.normcase(os.path.abspath(output_path)):
        return _fail(entry_id, "输出与输入相同：%s，拒绝覆盖源文件" % source_path)

    # 4. 源文件存在性（必须与"格式不支持"区分开）
    if not os.path.exists(source_path):
        return _fail(entry_id, "文件不存在：%s" % source_path)
    if not os.path.isfile(source_path):
        return _fail(entry_id, "源路径不是普通文件，无法读取：%s" % source_path)

    # 5. 格式校验（16位 PCM 双声道、8k~48k）
    try:
        wav = read_wav(source_path)
    except WavError as exc:
        return _fail(entry_id, str(exc))

    # 6. 时间越界校验（先在秒级判断，再在帧级复核）
    duration = wav.duration
    if end > duration + 1e-9:
        return _fail(
            entry_id,
            "时间越界：结束 %.3f 秒超过源文件时长 %.3f 秒" % (end, duration),
        )

    start_frame = int(round(start * wav.sample_rate))
    end_frame = int(round(end * wav.sample_rate))
    if end_frame > wav.frames:
        return _fail(
            entry_id,
            "时间越界：结束帧 %d 超过源文件总帧数 %d（源时长 %.3f 秒）"
            % (end_frame, wav.frames, duration),
        )
    if start_frame >= end_frame:
        return _fail(
            entry_id,
            "时间区间非法：起始帧 %d 不小于结束帧 %d，裁剪结果为空" % (start_frame, end_frame),
        )

    actual_duration = (end_frame - start_frame) / wav.sample_rate
    if abs(actual_duration - (end - start)) > DURATION_TOLERANCE:
        return _fail(
            entry_id,
            "裁剪时长偏差 %.4f 秒超过允许的 %.3f 秒"
            % (abs(actual_duration - (end - start)), DURATION_TOLERANCE),
        )

    # 7. 裁剪 -> RMS 增益对齐（含削波回退警告）
    clip = slice_samples(wav.samples, wav.channels, start_frame, end_frame)
    try:
        gained, warnings = apply_gain(clip, target_dbfs)
    except WavError as exc:
        return _fail(entry_id, str(exc))

    out_wav = type(wav)(
        sample_rate=wav.sample_rate,
        channels=wav.channels,
        sampwidth=wav.sampwidth,
        frames=(end_frame - start_frame),
        samples=gained,
    )

    # 8. 写出（直接写目标路径，不产生临时文件）
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        write_wav(output_path, out_wav)
    except OSError as exc:
        return _fail(entry_id, "输出 WAV 写入失败：%s（%s）" % (output_path, exc))

    return {
        "id": entry_id,
        "status": "成功",
        "output_path": output_path,
        "error": None,
        "warnings": warnings,
        "measured_rms_dbfs": round(rms_dbfs(gained), 3),
        "duration_seconds": round(actual_duration, 6),
    }


def process_manifest(manifest_path: str, out_dir: str, report_path: str) -> tuple[dict, int]:
    """执行整份清单。

    返回 (报告对象, 进程退出码)：
    - 全局失败（清单本身不可用）：退出码 1，报告含 global_status=失败；
    - 清单可用：逐行独立处理，全部成功退出码 0，存在条目失败退出码 2。
    """
    manifest_path = os.path.abspath(manifest_path)
    out_dir = os.path.abspath(out_dir)
    report_path = os.path.abspath(report_path)
    base_dir = os.path.dirname(manifest_path)

    try:
        rows = _load_rows(manifest_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        report = {
            "global_status": "失败",
            "manifest": manifest_path,
            "output_dir": out_dir,
            "error": str(exc),
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "entries": [],
        }
        _write_report(report_path, report)
        return report, 1

    entries = [_process_row(row, base_dir, out_dir) for row in rows]
    succeeded = sum(1 for e in entries if e["status"] == "成功")
    failed = len(entries) - succeeded
    report = {
        "global_status": "成功" if failed == 0 else "部分成功",
        "manifest": manifest_path,
        "output_dir": out_dir,
        "error": None,
        "total": len(entries),
        "succeeded": succeeded,
        "failed": failed,
        "entries": entries,  # 严格保持清单行顺序
    }
    _write_report(report_path, report)
    return report, (0 if failed == 0 else 2)


def _write_report(report_path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
