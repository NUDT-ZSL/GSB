#!/usr/bin/env python3
"""test_acceptance — 对 wav_trim.py 逐项执行 9 条验收并打印结果。

用法: python test_acceptance.py
退出码: 0 全部通过;1 存在失败项。
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import wave
from array import array

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "wav_trim.py")
GEN = os.path.join(ROOT, "make_samples.py")
SR = 44100

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("[%s] %s %s" % ("PASS" if cond else "FAIL", name, detail))


def run(args, cwd=None):
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True, cwd=cwd or ROOT)


def read_samples(path):
    with wave.open(path, "rb") as w:
        params = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        n = w.getnframes()
        data = w.readframes(n)
    a = array("h")
    a.frombytes(data)
    return params, n, a


def clamp16(v):
    return max(-32768, min(32767, v))


def main():
    tmp = tempfile.mkdtemp(prefix="wavtrim_test_")
    samples = os.path.join(tmp, "samples")
    subprocess.run([sys.executable, GEN, samples], check=True, capture_output=True)
    tone = os.path.join(samples, "tone5s.wav")
    loud = os.path.join(samples, "loud09.wav")
    silence = os.path.join(samples, "silence.wav")
    mono8 = os.path.join(samples, "mono8bit.wav")
    big = os.path.join(samples, "big.wav")

    # ---------- 1. 基本命令行 + 两个包络 ----------
    out1 = os.path.join(tmp, "case1", "out.wav")
    r = run([tone, out1, "--trim", "0.5", "4.5",
             "--envelope", "1.0,2.0,1.5", "--envelope", "3.0,4.0,0.5"])
    rep1 = os.path.join(os.path.dirname(out1), "report.json")
    check("1.两个包络示例命令成功生成输出与报告",
          r.returncode == 0 and os.path.exists(out1) and os.path.exists(rep1),
          "exit=%d" % r.returncode)

    # ---------- 2. 非法格式 ----------
    out2 = os.path.join(tmp, "case2", "out.wav")
    r = run([mono8, out2])
    check("2. 8位单声道报 unsupported format、非零退出、无输出",
          r.returncode != 0 and "unsupported format" in (r.stderr + r.stdout)
          and not os.path.exists(out2),
          "exit=%d stderr=%s" % (r.returncode, r.stderr.strip()[:80]))

    # ---------- 3. 剪切越界钳制 + 倒序报错 ----------
    out3a = os.path.join(tmp, "case3", "a.wav")
    r = run([tone, out3a, "--trim", "2", "10"])
    rep = json.load(open(os.path.join(os.path.dirname(out3a), "report.json"),
                         encoding="utf-8"))
    ok_clamp = (r.returncode == 0 and rep["trim"] == {"start": 2.0, "end": 5.0}
                and abs(rep["duration"] - 3.0) < 1e-9)
    out3b = os.path.join(tmp, "case3", "b.wav")
    r2 = run([tone, out3b, "--trim", "-5", "2"])
    rep2 = json.load(open(os.path.join(os.path.dirname(out3b), "report.json"),
                          encoding="utf-8"))
    ok_clamp2 = r2.returncode == 0 and rep2["trim"] == {"start": 0.0, "end": 2.0}
    out3c = os.path.join(tmp, "case3", "c.wav")
    r3 = run([tone, out3c, "--trim", "4", "1"])
    check("3. 越界剪切按边界限制(2-10->2-5, -5-2->0-2),倒序非零退出",
          ok_clamp and ok_clamp2 and r3.returncode != 0 and not os.path.exists(out3c),
          "clamp=%s clamp2=%s reversed_exit=%d" % (ok_clamp, ok_clamp2, r3.returncode))

    # ---------- 4. 重叠包络相乘 + 越界增益钳位 + 未覆盖区不变 ----------
    out4 = os.path.join(tmp, "case4", "out.wav")
    r = run([tone, out4, "--envelope", "1,3,1.5", "--envelope", "2,4,0.5",
             "--envelope", "4,5,9.9"])   # 9.9 应被钳位到 2.0
    _, _, src = read_samples(tone)
    _, _, dst = read_samples(out4)

    def expect(i):  # 帧 i(每帧 2 样本)的期望增益
        t = i / SR
        g = 1.0
        if 1.0 <= t < 3.0:
            g *= 1.5
        if 2.0 <= t < 4.0:
            g *= 0.5
        if 4.0 <= t < 5.0:
            g *= 2.0
        return g

    ok4 = r.returncode == 0
    for i in list(range(0, SR, 997)) + list(range(SR, 2 * SR, 991)) \
            + list(range(2 * SR, 3 * SR, 983)) + list(range(3 * SR, 4 * SR, 977)) \
            + list(range(4 * SR, 5 * SR, 971)):
        g = expect(i)
        for ch in range(2):
            s = src[2 * i + ch]
            want = clamp16(int(round(s * g)))
            if dst[2 * i + ch] != want:
                ok4 = False
    # 区间边界 [start, end) 语义:帧 44100(t=1.0)生效,帧 132300(t=3.0)不再乘 1.5
    s = src[2 * 44100]
    ok4 = ok4 and dst[2 * 44100] == clamp16(int(round(s * 1.5)))
    s = src[2 * 132300]
    ok4 = ok4 and dst[2 * 132300] == clamp16(int(round(s * 0.5)))
    check("4. 重叠区增益相乘、越界增益钳位到2.0、未覆盖区逐样本不变",
          ok4, "exit=%d" % r.returncode)

    # ---------- 5. 饱和截断不回绕 ----------
    out5 = os.path.join(tmp, "case5", "out.wav")
    r = run([loud, out5, "--envelope", "0,0.5,2.0"])
    _, _, d5 = read_samples(out5)
    check("5. 幅度0.9*增益2.0: 正=32767 负=-32768,无回绕",
          r.returncode == 0 and max(d5) == 32767 and min(d5) == -32768,
          "max=%d min=%d" % (max(d5), min(d5)))

    # ---------- 6. 输出参数一致、帧数误差<=1 ----------
    p_in, n_in, _ = read_samples(tone)
    p_out, n_out, _ = read_samples(out3a)
    check("6. wave 可读取输出,采样率/声道/位深一致,帧数误差<=1",
          p_in == p_out and abs(n_out - 3.0 * SR) <= 1,
          "params=%s frames=%d expect=%d" % (p_out, n_out, 3 * SR))

    # ---------- 7. report.json 字段 + 静音报错 ----------
    rep = json.load(open(rep1, encoding="utf-8"))
    ok_rep = (rep["input_path"].endswith("tone5s.wav")
              and rep["output_path"].endswith("out.wav")
              and rep["trim"] == {"start": 0.5, "end": 4.5}
              and abs(rep["duration"] - 4.0) < 1e-9
              and rep["envelopes"] == [
                  {"start": 1.0, "end": 2.0, "gain": 1.5},
                  {"start": 3.0, "end": 4.0, "gain": 0.5}])
    out7 = os.path.join(tmp, "case7", "out.wav")
    r = run([silence, out7])
    ok_sil = (r.returncode != 0 and "输入音频为静音" in (r.stderr + r.stdout)
              and not os.path.exists(out7))
    check("7. report.json 字段正确;静音输入报错且无输出",
          ok_rep and ok_sil, "report=%s silence=%s" % (ok_rep, ok_sil))

    # ---------- 8. 用法错误:退出码 2 + usage + 无新增文件 ----------
    dir8 = os.path.join(tmp, "case8")
    os.makedirs(dir8)
    before = set(os.listdir(dir8))
    o = os.path.join(dir8, "x.wav")
    cases8 = [
        run([]),                                            # 缺参数
        run([tone]),                                        # 缺输出
        run([tone, o, "--envelope", "1,2"]),                # 三元组不完整
        run([tone, o, "--envelope", "1,2,abc"]),            # 增益非数字
        run([tone, o, "--trim", "x", "2"]),                 # 剪切非数字
    ]
    ok8 = all(c.returncode == 2 and "usage" in (c.stderr + c.stdout).lower()
              for c in cases8)
    after = set(os.listdir(dir8))
    check("8. 缺参/包络不完整/增益非数字 -> 退出码2 + usage + 无输出文件",
          ok8 and before == after,
          "exits=%s new_files=%s" % ([c.returncode for c in cases8], after - before))

    # ---------- 9. 10MB 级输入性能 ----------
    size_mb = os.path.getsize(big) / 1e6
    out9 = os.path.join(tmp, "case9", "out.wav")
    t0 = time.perf_counter()
    r = run([big, out9, "--trim", "1", "59", "--envelope", "10,20,1.5"])
    elapsed = time.perf_counter() - t0
    try:
        p9, n9, _ = read_samples(out9)
        ok9 = (r.returncode == 0 and p9 == (SR, 2, 2)
               and abs(n9 - 58 * SR) <= 1 and elapsed < 30)
    except wave.Error:
        ok9 = False
    check("9. %.1fMB 输入 %.1fs 内完成,输出可读" % (size_mb, elapsed), ok9,
          "exit=%d" % r.returncode)

    # ---------- 汇总 ----------
    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n==== %d/%d 项通过 ====" % (len(RESULTS) - len(failed), len(RESULTS)))
    for n in failed:
        print("FAILED:", n)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
