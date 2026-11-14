#!/usr/bin/env python3
"""鼓组多轨对齐工具。

以基准轨为准，用互相关测算各分轨实际起始偏移，与描述 JSON 中的期望偏移比较：
- 输出逐轨对齐报告（按分轨名升序，含偏移误差、是否需返工、备注）；
- 为每条可用分轨输出对齐后的 WAV（<原轨名>_aligned.wav），长度与基准轨相差不超过一个采样帧；
- 偏移误差超过 5ms 的分轨在报告中标注为需返工；
- 文件缺失/损坏、采样率不符的分轨单独标注，不影响其余分轨；
- 生成可导入剪辑软件的片段包（对齐 WAV + 汇总清单 manifest.csv）。

用法: python align_tracks.py --input samples --output output
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

REWORK_THRESHOLD_MS = 5.0   # 偏移误差超过该值判定需返工
MAX_OFFSET_S = 2.0          # 互相关搜索的最大偏移范围（秒）


def to_mono(data: np.ndarray) -> np.ndarray:
    arr = np.asarray(data, dtype=np.float64)
    return arr if arr.ndim == 1 else arr.mean(axis=1)


def measure_lag(ref: np.ndarray, trk: np.ndarray, sr: int) -> int:
    """返回分轨相对基准轨的延迟（采样数）。trk[n] ≈ ref[n - lag]。"""
    n = len(ref) + len(trk) - 1
    nfft = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(trk, nfft) * np.conj(np.fft.rfft(ref, nfft)), nfft)
    max_lag = min(int(MAX_OFFSET_S * sr), len(ref) - 1, len(trk) - 1)
    cands = np.concatenate([corr[:max_lag + 1], corr[nfft - max_lag:]])
    idx = int(np.argmax(cands))
    return idx if idx <= max_lag else idx - len(cands)


def extract_aligned(trk: np.ndarray, lag: int, length: int) -> np.ndarray:
    """从分轨中截取与基准轨对齐、等长的片段（不足补零，保证长度恰好为 length）。"""
    shape = (length,) + trk.shape[1:]
    out = np.zeros(shape, dtype=np.float64)
    src_start = max(lag, 0)
    dst_start = max(-lag, 0)
    n = min(len(trk) - src_start, length - dst_start)
    if n > 0:
        out[dst_start:dst_start + n] = trk[src_start:src_start + n]
    return out


def fmt_ms(value: float | None) -> str:
    return "未知" if value is None else f"{value:+.2f}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="鼓组多轨对齐工具")
    ap.add_argument("--input", required=True, help="输入目录（含 tracks.json、基准轨与分轨 WAV）")
    ap.add_argument("--output", required=True, help="输出目录（片段包，须与输入目录分离）")
    args = ap.parse_args()

    input_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    if input_dir == output_dir:
        print("错误: 输出目录必须与输入目录分离", file=sys.stderr)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1) 读取描述 JSON ----
    with open(input_dir / "tracks.json", encoding="utf-8") as f:
        desc = json.load(f)
    desc_sr = desc.get("sample_rate")
    tracks = desc.get("tracks", [])
    print(f"描述文件含 {len(tracks)} 条分轨:")
    for t in tracks:
        exp = t.get("expected_offset_ms")
        print(f"  分轨 {t['name']}: 期望偏移 {'未知' if exp is None else f'{exp:.2f} ms'}")

    ref_data, ref_sr = sf.read(input_dir / desc.get("reference", "reference.wav"),
                               dtype="float64", always_2d=True)
    ref_mono = to_mono(ref_data)
    if desc_sr is not None and ref_sr != desc_sr:
        print(f"警告: 基准轨采样率 {ref_sr} Hz 与描述 {desc_sr} Hz 不符，以基准轨实际值为准")
    sr = ref_sr

    # ---- 2) 逐轨测算偏移 ----
    rows = []  # dict: name, measured, expected, error, rework, note, clip
    for t in tracks:
        name = t["name"]
        expected = t.get("expected_offset_ms")
        row = {"name": name, "measured": None, "expected": expected,
               "error": None, "rework": False, "note": "", "clip": ""}
        rows.append(row)

        wav_path = input_dir / t.get("file", f"{name}.wav")
        if not wav_path.is_file():
            row["note"] = "文件缺失或损坏，跳过"
            continue
        try:
            data, file_sr = sf.read(wav_path, dtype="float64", always_2d=True)
        except Exception as exc:
            row["note"] = f"文件缺失或损坏（{exc}），跳过"
            continue

        if desc_sr is not None and file_sr != desc_sr:
            row["note"] = f"采样率不符（实际 {file_sr} Hz，描述 {desc_sr} Hz），该轨不可用"
            continue
        if "channels" in t and data.shape[1] != t["channels"]:
            row["note"] = f"通道数与描述不符（实际 {data.shape[1]}，描述 {t['channels']}），仍继续处理"

        lag = measure_lag(ref_mono, to_mono(data), sr)
        measured_ms = lag * 1000.0 / sr
        row["measured"] = measured_ms

        if expected is None:
            row["note"] = (row["note"] + "；" if row["note"] else "") + "期望偏移未知，无法判定误差"
        else:
            error = measured_ms - expected
            row["error"] = error
            if abs(error) > REWORK_THRESHOLD_MS:
                row["rework"] = True

        # ---- 3) 输出对齐片段（长度与基准轨一致，起点为实测偏移处）----
        aligned = extract_aligned(data, lag, len(ref_mono))
        clip_name = f"{name}_aligned.wav"
        sf.write(output_dir / clip_name, aligned, sr, subtype="PCM_16")
        row["clip"] = clip_name

    rows.sort(key=lambda r: r["name"])

    # ---- 4) 对齐报告（按分轨名升序）----
    header = f"{'分轨名':<12}{'实测偏移(ms)':>12}{'期望偏移(ms)':>13}{'误差(ms)':>10}{'需返工':>7}  备注"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r['name']:<12}{fmt_ms(r['measured']):>12}{fmt_ms(r['expected']):>13}"
            f"{fmt_ms(r['error']):>10}{('是' if r['rework'] else '否'):>7}  {r['note']}"
        )
    report_text = "\n".join(lines)
    print("\n逐轨对齐报告:")
    print(report_text)
    (output_dir / "alignment_report.txt").write_text(report_text + "\n", encoding="utf-8")

    # ---- 5) 汇总清单（原分轨名与偏移）----
    with open(output_dir / "manifest.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["原分轨名", "实测偏移(ms)", "期望偏移(ms)", "误差(ms)", "需返工", "片段文件", "备注"])
        for r in rows:
            w.writerow([
                r["name"],
                "" if r["measured"] is None else f"{r['measured']:.2f}",
                "" if r["expected"] is None else f"{r['expected']:.2f}",
                "" if r["error"] is None else f"{r['error']:.2f}",
                "是" if r["rework"] else "否",
                r["clip"],
                r["note"],
            ])

    aligned = sum(1 for r in rows if r["clip"])
    rework = sum(1 for r in rows if r["rework"])
    unusable = sum(1 for r in rows if not r["clip"])
    print(f"\n片段包路径: {output_dir}")
    print(f"汇总: 对齐片段 {aligned} 个, 需返工 {rework} 个, 缺失/不可用 {unusable} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
