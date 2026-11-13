# -*- coding: utf-8 -*-
"""播客广告拼接工具。

读取 16 位 PCM 单声道 WAV 节目与广告，按插入计划 JSON 把广告重复插入节目，
输出采样点级拼接的 WAV 与 JSON 报告。

用法:
    python -I ad_inserter.py \
        --program program.wav --ad ad.wav \
        --plan plan.json --outdir "输出 目录"

输出文件:
    <outdir>/output.wav    拼接后的 WAV（采样率/位深/声道与输入一致）
    <outdir>/report.json   插入报告（起止采样点、编号、实际/未插入次数等）

设计要点:
- 所有输入校验在写入任何文件之前完成；校验失败时不产生输出文件。
- 输出 WAV 时长不超过原节目时长：插入的广告会挤占等长的节目尾部内容，
  放不下的广告整段跳过（不会插入半段广告，也不产生静音填充）。
- 采样点起止索引均从 0 开始；end_sample 为该广告最后一个采样点的索引（含）。
- 广告编号从 1 开始，按计划中有效插入项的出现顺序（计划顺序）编号。
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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


OUTPUT_WAV_NAME = "output.wav"
REPORT_JSON_NAME = "report.json"


class PlanError(Exception):
    """插入计划存在致命结构错误（缺字段、时间非数字、JSON 无法解析等）。"""


class AudioError(Exception):
    """音频文件不符合要求（不存在、非 WAV、非 16 位 PCM 单声道、采样率不一致等）。"""


@dataclass
class WaveData:
    params: wave._wave_params
    samples: array.array  # 有符号 16 位采样值数组

    @property
    def n_frames(self) -> int:
        return len(self.samples)

    @property
    def framerate(self) -> int:
        return self.params.framerate


# --------------------------------------------------------------------------- #
# WAV 读取与写入
# --------------------------------------------------------------------------- #
def read_wav(path: str) -> WaveData:
    if not os.path.isfile(path):
        raise AudioError(f"音频文件不存在: {path}")
    try:
        with wave.open(path, "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(params.nframes)
    except wave.Error as exc:
        raise AudioError(f"无法解析 WAV 文件 {path}: {exc}") from exc

    if params.comptype != "NONE":
        raise AudioError(
            f"文件 {path} 不是 PCM 格式（压缩类型={params.comptype}），仅支持 16 位 PCM"
        )
    if params.sampwidth != 2:
        raise AudioError(
            f"文件 {path} 位深为 {params.sampwidth * 8} 位，仅支持 16 位 PCM"
        )
    if params.nchannels != 1:
        raise AudioError(
            f"文件 {path} 声道数为 {params.nchannels}，仅支持单声道"
        )

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":  # WAV 一律小端
        samples.byteswap()
    return WaveData(params=params, samples=samples)


def write_wav(path: str, params: wave._wave_params, samples: array.array) -> None:
    """先写临时文件再原子替换，保证失败时不会破坏已有的同名文件。"""
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".wav.tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as fh:
            with wave.open(fh, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(params.framerate)
                wf.writeframes(samples.tobytes())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".json.tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# 计划解析与校验
# --------------------------------------------------------------------------- #
def _is_real_number(value: Any) -> bool:
    """真数字且有限（排除 bool、字符串、NaN、Infinity）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def load_plan(path: str) -> List[Dict[str, Any]]:
    """加载并做结构性校验。语义非法项（负值/越界/次数非正整数）留给调用方逐项标记。"""
    if not os.path.isfile(path):
        raise PlanError(f"计划文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PlanError(f"计划 JSON 无法解析（{exc.msg}，第 {exc.lineno} 行第 {exc.colno} 列）") from exc
    except OSError as exc:
        raise PlanError(f"计划文件无法读取: {exc}") from exc

    if not isinstance(data, dict) or "insertions" not in data:
        raise PlanError("计划 JSON 必须是包含 'insertions' 字段的对象")
    insertions = data["insertions"]
    if not isinstance(insertions, list):
        raise PlanError("计划字段 'insertions' 必须是数组")

    for index, item in enumerate(insertions):
        where = f"第 {index + 1} 项"
        if not isinstance(item, dict):
            raise PlanError(f"{where} 必须是对象")
        if "time_sec" not in item:
            raise PlanError(f"{where} 缺少必需字段 'time_sec'")
        if "repeats" not in item:
            raise PlanError(f"{where} 缺少必需字段 'repeats'")
        # 时间非数字属于致命错误（任务要求 8），在任何输出之前终止
        if not _is_real_number(item["time_sec"]):
            raise PlanError(
                f"{where} 的 time_sec 必须是数字，实际为 {type(item['time_sec']).__name__}: "
                f"{item['time_sec']!r}"
            )
    return insertions


def classify_entries(
    insertions: List[Dict[str, Any]], program_len: int, framerate: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """逐项做语义校验，返回 (有效项, 无效项)；无效项不中断其他插入。"""
    valid: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    duration_sec = program_len / framerate

    for index, item in enumerate(insertions):
        time_sec = item["time_sec"]
        repeats = item["repeats"]
        reason: Optional[str] = None

        if time_sec < 0:
            reason = f"插入点为负数（{time_sec}），必须 >= 0"
        elif time_sec > duration_sec:
            reason = (
                f"插入点 {time_sec}s 超出节目时长 {duration_sec:.6f}s"
                f"（节目共 {program_len} 个采样点）"
            )
        elif not isinstance(repeats, int) or isinstance(repeats, bool) or repeats <= 0:
            reason = f"重复次数必须是正整数，实际为 {repeats!r}"

        if reason is not None:
            invalid.append(
                {
                    "plan_index": index,
                    "time_sec": time_sec,
                    "repeats": repeats,
                    "reason": reason,
                }
            )
        else:
            insert_sample = int(round(time_sec * framerate))
            # 浮点误差保护：夹到合法区间内
            insert_sample = max(0, min(insert_sample, program_len))
            valid.append(
                {
                    "plan_index": index,
                    "time_sec": time_sec,
                    "repeats": repeats,
                    "insert_sample_in_program": insert_sample,
                }
            )
    return valid, invalid


# --------------------------------------------------------------------------- #
# 拼接
# --------------------------------------------------------------------------- #
# 块表示: (类型, 起始, 长度, 归属信息)
#   节目块 ("program", start_in_program, length, None)
#   广告块 ("ad",      0,                length, (plan_index, copy_index))
Block = Tuple[str, int, int, Optional[Tuple[int, int]]]


def build_output(
    program: WaveData,
    ad: WaveData,
    valid_entries: List[Dict[str, Any]],
) -> Tuple[array.array, List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """按插入位置依次拼接，输出长度不超过原节目长度。

    调度按插入位置排序进行，保证先前放入的广告不会被后续、更靠前的插入点
    推出边界（任何被截断的广告都不允许出现）。报告编号与顺序仍按计划顺序。
    """
    program_len = program.n_frames
    ad_len = ad.n_frames
    blocks: List[Block] = [("program", 0, program_len, None)]

    # 调度结果先挂在有效项上，最终再按计划顺序整理
    scheduled: Dict[int, Dict[str, Any]] = {}

    # 同一插入点保持计划顺序；整体按插入位置排序
    ordered = sorted(valid_entries, key=lambda e: (e["insert_sample_in_program"], e["plan_index"]))

    for entry in ordered:
        plan_index = entry["plan_index"]
        at = entry["insert_sample_in_program"]
        requested = entry["repeats"]

        # 找到包含该插入点的节目块（插入点等于块尾时落在块边界）
        target_idx = -1
        cursor = 0
        for idx, block in enumerate(blocks):
            kind, start, length, _owner = block
            end = cursor + length
            if kind == "program" and cursor <= at <= end:
                target_idx = idx
                block_start, block_end = cursor, end
                break
            cursor = end
        if target_idx < 0:
            # 插入点落在广告块内（防御性分支：有效插入点只会落在节目块上）
            scheduled[plan_index] = {
                "actual": 0,
                "skipped": requested,
                "copies": [],
                "insert_sample_in_program": at,
                "time_sec": entry["time_sec"],
                "requested": requested,
            }
            continue

        # 剩余空间 = 从当前输出位置到节目原始末尾的容量
        remaining = program_len - block_start
        fit = remaining // ad_len if ad_len > 0 else 0
        actual = min(requested, max(0, fit))

        kind, prog_start, prog_length, _owner = blocks[target_idx]
        offset = at - block_start
        left_len = offset
        right_len = prog_length - offset
        new_blocks: List[Block] = blocks[:target_idx]
        if left_len > 0:
            new_blocks.append(("program", prog_start, left_len, None))
        copies: List[Tuple[int, int]] = []
        for copy_index in range(actual):
            owner = (plan_index, copy_index)
            new_blocks.append(("ad", 0, ad_len, owner))
            copies.append(owner)
        if right_len > 0:
            new_blocks.append(("program", prog_start + offset, right_len, None))
        new_blocks.extend(blocks[target_idx + 1 :])
        blocks = new_blocks

        scheduled[plan_index] = {
            "actual": actual,
            "skipped": requested - actual,
            "copies": copies,
            "insert_sample_in_program": at,
            "time_sec": entry["time_sec"],
            "requested": requested,
        }

    # 拉平为采样数组，并记录每段广告在输出中的起止位置
    output = array.array("h")
    ad_ranges: Dict[Tuple[int, int], Tuple[int, int]] = {}
    pos = 0
    program_samples_kept = 0
    for kind, start, length, owner in blocks:
        if pos >= program_len:
            break
        take = min(length, program_len - pos)
        if kind == "program":
            output.extend(program.samples[start : start + take])
            program_samples_kept += take
        else:
            ad_ranges[owner] = (pos, pos + take - 1)  # end 为含尾索引
            output.extend(ad.samples[start : start + take])
        pos += take

    # 被广告挤占/丢弃的节目尾部采样数
    truncated = program_len - program_samples_kept

    # 按计划顺序生成报告插入项；广告编号按计划顺序从 1 递增
    insert_details: List[Dict[str, Any]] = []
    request_summary: List[Dict[str, Any]] = []
    ad_number = 0
    for entry in valid_entries:
        plan_index = entry["plan_index"]
        info = scheduled[plan_index]
        numbers: List[int] = []
        for copy_index in range(info["actual"]):
            ad_number += 1
            numbers.append(ad_number)
            out_start, out_end = ad_ranges[(plan_index, copy_index)]
            insert_details.append(
                {
                    "ad_number": ad_number,
                    "plan_index": plan_index,
                    "plan_time_sec": entry["time_sec"],
                    "copy_index": copy_index + 1,
                    "insert_sample_in_program": info["insert_sample_in_program"],
                    "start_sample": out_start,
                    "end_sample": out_end,
                    "sample_count": out_end - out_start + 1,
                }
            )
        # 每个有效计划项一条请求摘要：请求次数 / 实际插入 / 未插入（容量不足）
        request_summary.append(
            {
                "plan_index": plan_index,
                "plan_time_sec": entry["time_sec"],
                "insert_sample_in_program": info["insert_sample_in_program"],
                "requested_repeats": entry["repeats"],
                "actual_inserted": info["actual"],
                "not_inserted": info["skipped"],
                "ad_numbers": numbers,
            }
        )
    return output, insert_details, truncated, request_summary


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run(program_path: str, ad_path: str, plan_path: str, outdir: str) -> Dict[str, Any]:
    # 1) 音频校验（全部在写文件之前）
    program = read_wav(program_path)
    ad = read_wav(ad_path)
    if program.framerate != ad.framerate:
        raise AudioError(
            f"采样率不一致: 节目 {program.framerate} Hz，广告 {ad.framerate} Hz"
        )

    # 2) 计划结构校验（致命错误在此抛出，不写任何文件）
    raw_plan = load_plan(plan_path)

    # 3) 计划项语义校验（无效项标记跳过，不中断有效项）
    valid_entries, invalid_entries = classify_entries(
        raw_plan, program.n_frames, program.framerate
    )

    # 4) 拼接
    output_samples, insert_details, truncated, request_summary = build_output(program, ad, valid_entries)

    report: Dict[str, Any] = {
        "sample_rate_hz": program.framerate,
        "bits_per_sample": 16,
        "channels": 1,
        "program_total_samples": program.n_frames,
        "ad_total_samples": ad.n_frames,
        "output_total_samples": len(output_samples),
        "inserted_segment_count": len(insert_details),
        "truncated_program_tail_samples": truncated,
        "insertion_requests": request_summary,
        "insertions": insert_details,
        "skipped_invalid_items": invalid_entries,
    }

    wav_path = os.path.join(outdir, OUTPUT_WAV_NAME)
    report_path = os.path.join(outdir, REPORT_JSON_NAME)
    write_wav(wav_path, program.params, output_samples)
    write_json_atomic(report_path, report)
    return {"report": report, "wav_path": wav_path, "report_path": report_path}


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认 GBK，强制标准输出走 UTF-8，保证中文摘要可复现
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="把赞助商广告按插入计划拼接进 16 位 PCM 单声道 WAV 节目"
    )
    parser.add_argument("--program", required=True, help="节目 WAV 路径")
    parser.add_argument("--ad", required=True, help="广告 WAV 路径")
    parser.add_argument("--plan", required=True, help="插入计划 JSON 路径")
    parser.add_argument("--outdir", required=True, help="输出目录（可含空格）")
    args = parser.parse_args(argv)

    try:
        result = run(args.program, args.ad, args.plan, args.outdir)
    except (AudioError, PlanError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    report = result["report"]
    print("处理完成")
    print(f"插入段数: {report['inserted_segment_count']}")
    print(f"输出 WAV: {os.path.abspath(result['wav_path'])}")
    print(f"输出报告: {os.path.abspath(result['report_path'])}")
    print(f"节目总采样点: {report['program_total_samples']}，输出总采样点: {report['output_total_samples']}")
    if report["skipped_invalid_items"]:
        print(f"跳过的无效计划项: {len(report['skipped_invalid_items'])} 项（详见报告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
