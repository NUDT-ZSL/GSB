#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立校验 demo 产出（仅标准库，与 audio_paste.py 无任何代码共享）。

校验内容：
  case1 插入模式：
    - 输出头：单声道 / 16bit / 8000Hz，与目标一致
    - 时长 = 1.000 + (0.500 - 0.100) = 1.400s = 11200 帧
    - 字节布局：目标[:4800] + 源[800:] + 目标[4800:]
    - 标注：插入点及之前不变、之后平移 0.400s、源标注按相对位置插入、
      整体按 (time, label) 排序
  case2 追加模式：
    - 时长 = 1.000 + 0.500 = 1.500s = 12000 帧，字节布局 = 目标 + 源
    - 原目标标注时间全部不变
运行： python -I demo\\verify_demo.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import wave
from array import array
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "demo" / "data"
OUT = ROOT / "demo" / "out"


def load_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate(),
                  wf.getnframes(), wf.getcomptype())
        frames = wf.readframes(wf.getnframes())
    return params, array("h", frames), frames


def approx(a, b, eps=1e-12):
    return abs(a - b) <= eps


def main() -> int:
    failures = 0

    t_param, t_smp, t_raw = load_wav(DATA / "target.wav")
    s_param, s_smp, s_raw = load_wav(DATA / "source.wav")
    m1_param, m1_smp, _ = load_wav(OUT / "case1_mixed.wav")
    m2_param, m2_smp, _ = load_wav(OUT / "case2_mixed.wav")

    # ---- case1 WAV 头 ----
    print("== case1 插入模式 ==")
    expect_rate = t_param[2]
    ok = (m1_param[0] == 1 and m1_param[1] == 2 and m1_param[2] == expect_rate
          and m1_param[4] == "NONE")
    print(f"[{'OK' if ok else 'FAIL'}] 输出头: 单声道={m1_param[0]} "
          f"位深={m1_param[1] * 8}bit 采样率={m1_param[2]}Hz "
          f"压缩={m1_param[4]} (目标采样率 {expect_rate})")
    failures += not ok

    # ---- case1 时长 ----
    ok = m1_param[3] == 11200 and approx(m1_param[3] / m1_param[2], 1.400)
    print(f"[{'OK' if ok else 'FAIL'}] 时长: {m1_param[3]} 帧 / "
          f"{m1_param[3] / m1_param[2]:.3f}s (期望 11200 / 1.400s)")
    failures += not ok

    # ---- case1 字节级波形布局 ----
    insert_frame = round(0.600 * expect_rate)       # 4800
    src_start_frame = round(0.100 * expect_rate)    # 800
    expected_layout = (list(t_smp[:insert_frame])
                       + list(s_smp[src_start_frame:])
                       + list(t_smp[insert_frame:]))
    ok = list(m1_smp) == expected_layout
    print(f"[{'OK' if ok else 'FAIL'}] 波形字节布局: "
          f"目标[:4800]+源[800:]+目标[4800:] 完全一致")
    failures += not ok

    # ---- case1 标注 ----
    ann1 = json.loads((OUT / "case1_annotations.json").read_text(encoding="utf-8"))
    # 源标注映射规则：新时间 = 0.600 + (源时间 - 源起始点 0.100)；
    # 源 0.000 的标注早于源起始点，被丢弃；0.125->0.625，0.500->1.000。
    expected_ann1 = [
        (0.250, "目标-前段标记"),      # 插入点之前，原样
        (0.600, "目标-插入点处标记"),  # 恰好等于插入点，原样
        (0.625, "源-四分之一处"),      # 0.600 + (0.125-0.100)
        (1.000, "源-片段结尾"),        # 0.600 + (0.500-0.100) 片段末端
        (1.200, "目标-后段标记"),      # 0.800 + 0.400 平移
    ]
    got_ann1 = [(x["time"], x["label"]) for x in ann1]
    ok = got_ann1 == expected_ann1
    print(f"[{'OK' if ok else 'FAIL'}] 标注同步 (time,label):")
    for g, e in zip(got_ann1, expected_ann1):
        mark = "OK " if approx(g[0], e[0]) and g[1] == e[1] else "BAD"
        print(f"      {mark} 实际 {g}  期望 {e}")
    failures += not ok
    keys = [(x["time"], x["label"]) for x in ann1]
    ok = keys == sorted(keys)
    print(f"[{'OK' if ok else 'FAIL'}] 标注满足 (时间升序, 同刻标签字典序)")
    failures += not ok

    # ---- case2 追加模式 ----
    print("== case2 越界追加模式 ==")
    ok = m2_param[3] == 12000 and approx(m2_param[3] / m2_param[2], 1.500)
    print(f"[{'OK' if ok else 'FAIL'}] 时长: {m2_param[3]} 帧 / "
          f"{m2_param[3] / m2_param[2]:.3f}s (期望 12000 / 1.500s)")
    failures += not ok
    ok = list(m2_smp) == list(t_smp) + list(s_smp)
    print(f"[{'OK' if ok else 'FAIL'}] 波形字节布局: 目标+源 完全一致")
    failures += not ok
    ann2 = json.loads((OUT / "case2_annotations.json").read_text(encoding="utf-8"))
    orig = json.loads((DATA / "target_annotations.json").read_text(encoding="utf-8"))
    ok = [(x["time"], x["label"]) for x in ann2] == \
        [(x["time"], x["label"]) for x in orig]
    print(f"[{'OK' if ok else 'FAIL'}] 原目标标注时间全部不变: "
          f"{[(x['time'], x['label']) for x in ann2]}")
    failures += not ok

    # ---- 原始标注文件未被修改 ----
    print("== 输入文件只读性 ==")
    for name in ("target_annotations.json", "source_annotations.json"):
        h = hashlib.sha256((DATA / name).read_bytes()).hexdigest()[:16]
        print(f"[INFO] {name} sha256 前缀 {h}（与生成时一致即未被修改）")

    print(f"\n校验结果: {'全部通过' if failures == 0 else f'{failures} 项失败'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
