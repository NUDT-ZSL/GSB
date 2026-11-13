#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 podmaster.py 逐条执行 13 项验收，全部通过则退出码 0。"""
import json
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
OUT = os.path.join(HERE, "out_verify")
RATE = 44100

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run_cli(manifest, outdir):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "podmaster.py"),
         os.path.join(SAMPLES, manifest), "-o", outdir],
        capture_output=True, text=True)


def read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        return w.getframerate(), list(struct.unpack("<%dh" % n, raw))


def peak(samples, lo, hi):
    return max(abs(x) for x in samples[lo:hi])


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    od = lambda name: os.path.join(OUT, name)

    # ---- 1. 采样率不一致 → 非零退出，错误含采样率
    r = run_cli("manifest_badrate.json", od("t1"))
    check("1 采样率不一致拒绝处理",
          r.returncode != 0 and "采样率" in r.stderr
          and "44100" in r.stderr and "48000" in r.stderr,
          r.stderr.strip().splitlines()[0] if r.stderr else "")

    # ---- 2. +6dB 拼接无溢出；+12dB 越界样本钳制
    r = run_cli("manifest_valid.json", od("t2"))
    ok = r.returncode == 0
    if ok:
        _, s = read_wav(os.path.join(od("t2"), "master.wav"))
        ok = max(s) <= 32767 and min(s) >= -32768
    check("2a +6dB 拼接无溢出", ok)
    r = run_cli("manifest_clamp.json", od("t2b"))
    ok = r.returncode == 0
    if ok:
        _, s = read_wav(os.path.join(od("t2b"), "master.wav"))
        ok = max(s) == 32767 and min(s) == -32768
    check("2b 越界样本钳制在 ±32767/32768", ok)

    # ---- 3. 淡化 1000ms > 相邻 500ms → 报告非法
    r = run_cli("manifest_bad_fade.json", od("t3"))
    check("3 超长交叉淡化被报告非法",
          r.returncode != 0 and "淡化" in r.stderr and "1000" in r.stderr,
          r.stderr.strip().splitlines()[-1] if r.stderr else "")

    # ---- 4. 三条非法项 → 三条错误且不生成母带
    r = run_cli("manifest_bad_items.json", od("t4"))
    n_err = r.stderr.count("clips[")
    check("4 三条非法项逐条报告且不产出母带",
          r.returncode != 0 and n_err == 3
          and not os.path.exists(os.path.join(od("t4"), "master.wav")),
          f"错误条数={n_err}")

    # ---- 5. 同一片段两次引用、不同增益互不影响
    r = run_cli("manifest_reuse.json", od("t5"))
    ok = r.returncode == 0
    if ok:
        _, s = read_wav(os.path.join(od("t5"), "master.wav"))
        p1 = peak(s, 4410, 44100)          # 第一段中部（0dB）
        p2 = peak(s, 44100 + 4410, 88200)  # 第二段中部（-6dB）
        ratio = p2 / p1
        ok = abs(ratio - 10 ** (-6 / 20)) < 0.02
    check("5 同片段不同增益两次引用", ok, f"幅度比={ratio:.4f}（期望≈0.5012）" if ok else "")

    # ---- 6. 首尾淡入淡出，中间幅度保持
    _, s = read_wav(os.path.join(od("t2"), "master.wav"))
    mid = peak(s, len(s) // 2 - 2000, len(s) // 2 + 2000)
    check("6 首尾样本接近 0 且中段幅度保持",
          abs(s[0]) <= 1 and abs(s[-1]) <= 1 and mid > 10000,
          f"首={s[0]} 末={s[-1]} 中段峰值={mid}")

    # ---- 7. 版本 99 被拒，错误信息出现版本 1
    r = run_cli("manifest_v99.json", od("t7"))
    check("7 不支持的版本被拒绝",
          r.returncode != 0 and "99" in r.stderr and "1" in r.stderr and "版本" in r.stderr)

    # ---- 8. 两段 1000ms + 淡化 100ms → 约 1900ms
    r = run_cli("manifest_duration.json", od("t8"))
    _, s = read_wav(os.path.join(od("t8"), "master.wav"))
    dur = len(s) * 1000.0 / RATE
    check("8 母带时长=各段之和-淡化重叠", abs(dur - 1900.0) < 1.0, f"实际 {dur:.3f}ms")

    # ---- 9. JSON 报告段数与清单一致，总时长与 WAV 头一致
    rep = json.load(open(os.path.join(od("t2"), "report.json"), encoding="utf-8"))
    man = json.load(open(os.path.join(SAMPLES, "manifest_valid.json"), encoding="utf-8"))
    with wave.open(os.path.join(od("t2"), "master.wav")) as w:
        wav_ms = w.getnframes() * 1000.0 / w.getframerate()
    check("9 报告段数与总时长一致",
          len(rep["segments"]) == len(man["clips"])
          and abs(rep["total_duration_ms"] - wav_ms) < 0.1,
          f"报告 {rep['total_duration_ms']}ms / WAV {wav_ms:.3f}ms")

    # ---- 10. 截断/非单声道/非16位 WAV 报具体文件与原因
    r1 = run_cli("manifest_truncated.json", od("t10a"))
    r2 = run_cli("manifest_stereo.json", od("t10b"))
    r3 = run_cli("manifest_8bit.json", od("t10c"))
    check("10 坏 WAV 报文件与原因",
          r1.returncode != 0 and "truncated.wav" in r1.stderr and "截断" in r1.stderr
          and r2.returncode != 0 and "stereo.wav" in r2.stderr and "单声道" in r2.stderr
          and r3.returncode != 0 and "eightbit.wav" in r3.stderr and "16" in r3.stderr
          and not os.path.exists(os.path.join(od("t10a"), "master.wav")))

    # ---- 11. 缺字段/非法 JSON 指明字段名或位置
    r1 = run_cli("manifest_no_clips.json", od("t11a"))
    r2 = run_cli("manifest_broken.json", od("t11b"))
    check("11 清单错误指明字段或解析位置",
          r1.returncode != 0 and "clips" in r1.stderr
          and r2.returncode != 0 and "行" in r2.stderr and "列" in r2.stderr)

    # ---- 12. 重复运行覆盖；失败后无临时文件残留
    r1 = run_cli("manifest_valid.json", od("t12"))
    r2 = run_cli("manifest_valid.json", od("t12"))
    ok = r1.returncode == 0 and r2.returncode == 0
    if ok:
        _, s = read_wav(os.path.join(od("t12"), "master.wav"))
        ok = len(s) > 0
    run_cli("manifest_bad_items.json", od("t12"))  # 失败运行
    leftovers = [f for f in os.listdir(od("t12")) if f.endswith(".tmp")]
    check("12 重复运行覆盖且无半成品残留", ok and not leftovers,
          f"残留={leftovers}" if leftovers else "")

    # ---- 13. 退出码：合法 0 / 非法非 0 且 stderr 含原因
    check("13 退出码与 stderr", r.returncode == 0
          and run_cli("manifest_v99.json", od("t13")).returncode == 1
          and run_cli("manifest_v99.json", od("t13")).stderr.strip() != "")

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} 项通过")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
