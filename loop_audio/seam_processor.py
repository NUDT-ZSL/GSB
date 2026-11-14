# -*- coding: utf-8 -*-
"""音频循环接缝处理工具。

读取接缝点清单 JSON，逐条加载 WAV：
  * 仅接受 44100 Hz / 16 位 / 单声道 PCM WAV；
  * 对每个候选接缝计算前后各 5 ms 窗口的 RMS 差相对整体 RMS 的比值（接缝得分）；
  * 选取得分最低（并列时取清单靠前）的候选接缝；
  * 在接缝处做不超过 10 ms 的交叉淡化，导出与预期循环长度一致（误差 < 1 ms）的 WAV；
  * 生成与清单同序、含 file/status/seam_index/seam_score/reason 五字段的报告 JSON。

用法：
    python -I seam_processor.py --manifest manifest.json --report report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import soundfile as sf

try:  # Windows 控制台默认 GBK，统一以 UTF-8 输出避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SAMPLE_RATE = 44100          # 仅接受的采样率（Hz）
SAMPLE_WIDTH_BITS = 16       # 仅接受的位深
CHANNELS = 1                 # 仅接受单声道
WINDOW_MS = 5.0              # 接缝前后 RMS 窗口长度
CROSSFADE_MS = 10.0          # 导出时交叉淡化的最大长度
WINDOW_LEN = int(round(SAMPLE_RATE * WINDOW_MS / 1000.0))      # 220 个采样
CROSSFADE_LEN = int(round(SAMPLE_RATE * CROSSFADE_MS / 1000.0))  # 441 个采样
EPS = 1e-12


def rms(samples: np.ndarray) -> float:
    """计算一段整型 PCM 采样的 RMS，按 16 位满量程归一化。"""
    x = samples.astype(np.float64) / 32768.0
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


def seam_score(samples: np.ndarray, seam: int, overall_rms: float) -> float:
    """接缝得分：接缝前后各 5 ms 窗口的 RMS 差 / 整体 RMS。

    连续信号前后窗口能量接近 -> 得分低（< 0.2）；
    接缝跨在电平突变上 -> 得分高（> 0.8）。
    """
    pre = samples[seam - WINDOW_LEN:seam]
    post = samples[seam:seam + WINDOW_LEN]
    if pre.size < WINDOW_LEN or post.size < WINDOW_LEN:
        return float("nan")
    return float(abs(rms(pre) - rms(post)) / max(overall_rms, EPS))


def apply_wrap_crossfade(loop: np.ndarray, length: int) -> np.ndarray:
    """在循环首尾（即播放回绕的接缝处）做等功率线性交叉淡化。

    取循环尾部 fade 段与头部 fade 段，分别施加渐入/渐隐权重后相加，
    写回循环头部；长度保持不变（不改变输出时长），淡化长度不超过 10 ms。
    """
    fade_len = min(CROSSFADE_LEN, length // 2)
    if fade_len <= 1:
        return loop
    out = loop.astype(np.float64)
    head = out[:fade_len].astype(np.float64)
    tail = out[out.shape[0] - fade_len:].astype(np.float64)
    weights = np.arange(fade_len, dtype=np.float64) / (fade_len - 1)
    blended = tail * (1.0 - weights) + head * weights
    out[:fade_len] = blended
    return np.clip(np.rint(out), -32768, 32767).astype(np.int16)


def process_entry(entry: dict, base_dir: str, out_dir: str, seen_files: set) -> dict:
    """处理清单中的单个条目，返回五字段报告字典。"""
    file_name = str(entry.get("file", ""))
    report = {
        "file": file_name,
        "status": "skipped",
        "seam_index": None,
        "seam_score": None,
        "reason": "",
    }

    # 规则 8：重复文件名仅处理首个条目
    if file_name in seen_files:
        report["reason"] = "重复文件名，跳过后续条目"
        return report
    seen_files.add(file_name)

    wav_path = os.path.join(base_dir, file_name)

    # 规则 6/12：文件缺失或暂时不可读（被占用/锁定/损坏）
    if not os.path.isfile(wav_path):
        report["reason"] = "文件缺失，跳过该条目"
        return report

    try:
        info = sf.info(wav_path)
    except Exception as exc:  # soundfile 读取头部失败：损坏或被锁定
        report["reason"] = "文件暂时不可读（无法读取音频头）：%s" % exc
        return report

    # 规则 1：仅接受 44100Hz / 16位 / 单声道
    problems = []
    if info.samplerate != SAMPLE_RATE:
        problems.append("采样率为 %s Hz，不符合 %d Hz" % (info.samplerate, SAMPLE_RATE))
    if info.subtype != "PCM_16":
        problems.append("位深/子类型为 %s，不符合 16 位 PCM" % info.subtype)
    if info.channels != CHANNELS:
        problems.append("声道数为 %d，不符合单声道" % info.channels)
    if problems:
        report["reason"] = "格式不支持，跳过该条目：" + "；".join(problems)
        return report

    try:
        samples, sr = sf.read(wav_path, dtype="int16", always_2d=False)
    except Exception as exc:
        report["reason"] = "文件暂时不可读（音频数据读取失败）：%s" % exc
        return report

    if samples.ndim != 1:  # 双保险：非单声道数据
        report["reason"] = "格式不支持，跳过该条目：音频数据不是单声道"
        return report

    total = samples.shape[0]

    # 规则 7：预期循环长度超过实际长度
    expected_len = int(entry.get("expected_loop_length", 0))
    if expected_len <= 0:
        report["reason"] = "清单中预期循环长度无效"
        return report
    if expected_len > total:
        report["reason"] = "长度不足：预期循环长度 %d 采样，实际音频仅 %d 采样" % (
            expected_len,
            total,
        )
        return report

    # 规则 11：全零（静音）音频
    if not np.any(samples):
        report["seam_score"] = 1.0
        report["reason"] = "静音（全零音频），跳过且不产出导出文件"
        return report

    overall = rms(samples)
    candidates = entry.get("candidate_seams", [])
    if not isinstance(candidates, list) or not candidates:
        report["reason"] = "清单中没有候选接缝点"
        return report

    # 规则 2：逐个候选接缝计算得分（越界候选直接记为无效）
    scored = []
    for idx, seam in enumerate(candidates):
        seam = int(seam)
        if seam < WINDOW_LEN or seam > total - WINDOW_LEN:
            continue
        score = seam_score(samples, seam, overall)
        if np.isfinite(score):
            scored.append((idx, seam, score))

    if not scored:
        report["reason"] = "没有有效的候选接缝点（候选位置越界或窗口不足 5 ms）"
        return report

    # 规则 3：得分升序，并列时取清单靠前者（min 保持首次出现）
    chosen_idx, chosen_seam, chosen_score = min(scored, key=lambda item: item[2])

    # 规则 4：以选中接缝为循环起点，循环回绕处做 <=10ms 交叉淡化
    if chosen_seam + expected_len <= total:
        loop = samples[chosen_seam:chosen_seam + expected_len].copy()
    else:  # 源音频不足时允许回绕取数（本清单样例不会触发）
        loop = np.tile(samples, expected_len // total + 1)[
            chosen_seam:chosen_seam + expected_len
        ].copy()

    loop = apply_wrap_crossfade(loop, expected_len)

    os.makedirs(out_dir, exist_ok=True)  # 规则 9：输出目录不存在则创建
    out_path = os.path.join(out_dir, file_name)
    sf.write(out_path, loop, SAMPLE_RATE, subtype="PCM_16")

    report["status"] = "ok"
    report["seam_index"] = chosen_idx
    report["seam_score"] = round(float(chosen_score), 6)
    report["reason"] = ""
    return report


def run(manifest_path: str, report_path: str | None = None) -> list:
    """读取清单并处理全部条目，写出报告 JSON，返回报告列表。"""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    out_dir = manifest.get("output_dir", "out")
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(base_dir, out_dir)
    os.makedirs(out_dir, exist_ok=True)

    entries = manifest.get("entries", [])
    report_rows = []
    seen_files: set = set()

    # 规则 5：报告按清单顺序；规则 10：控制台逐条输出进度
    for entry in entries:
        row = process_entry(entry, base_dir, out_dir, seen_files)
        report_rows.append(row)
        print(
            "[%s] %s%s"
            % (
                row["status"].upper(),
                row["file"],
                ""
                if row["status"] == "ok"
                else " -> " + (row["reason"] or "跳过"),
            ),
            flush=True,
        )

    if report_path is None:
        report_path = os.path.join(base_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report_rows, fh, ensure_ascii=False, indent=2)
    print("报告已写入：%s（共 %d 条，成功 %d 条）"
          % (report_path, len(report_rows),
             sum(1 for r in report_rows if r["status"] == "ok")), flush=True)
    return report_rows


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="循环音效接缝评估与导出工具")
    parser.add_argument("--manifest", required=True, help="接缝点清单 JSON 路径")
    parser.add_argument("--report", default=None, help="报告 JSON 输出路径（默认清单目录下 report.json）")
    args = parser.parse_args(argv)
    run(args.manifest, args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
