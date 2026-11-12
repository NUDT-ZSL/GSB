#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_acceptance.py -- 12 项验收的自动验证。

所有参照值(时间轴真值、混音结果、归一化结果)均由本文件用独立的
Fraction/整数实现重新计算,不与 engine.py 共享代码路径。
运行: python run_acceptance.py
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testgen

ROOT = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(ROOT, "engine.py")
TD = os.path.join(ROOT, "testdata")
S24_MIN, S24_MAX = -(1 << 23), (1 << 23) - 1
SR = 48000
QUARTER = SR * 60 // 120          # bpm=120 时每四分音符的采样数 = 24000

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", name,
                         "" if cond else ("  -- " + detail)))


def run(args):
    return subprocess.run([sys.executable, "-I", ENGINE] + args,
                          capture_output=True, text=True, cwd=ROOT)


def last_json(stdout):
    lines = [l for l in stdout.strip().splitlines() if l.strip()]
    return json.loads(lines[-1])


def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


# ---- 独立参照实现(不依赖 engine.py) ----

def idecode(buf):
    return [int.from_bytes(buf[i:i + 3], "little", signed=True)
            for i in range(0, len(buf), 3)]


def iencode(vals):
    return b"".join((v & 0xFFFFFF).to_bytes(3, "little") for v in vals)


def irhalf(n, d):
    if n >= 0:
        return (2 * n + d) // (2 * d)
    return -((-2 * n + d) // (2 * d))


def iclamp(v):
    return S24_MIN if v < S24_MIN else (S24_MAX if v > S24_MAX else v)


def truth_samples(segs, q, sr):
    acc = Fraction(0)
    for i, (s, b) in enumerate(segs):
        nxt = segs[i + 1][0] if i + 1 < len(segs) else None
        if nxt is not None and q >= nxt:
            acc += Fraction(60 * sr) * (nxt - s) / b
        else:
            acc += Fraction(60 * sr) * (q - s) / b
            break
    return acc


def q(frames):
    """采样帧数 -> 四分音符位置字符串(bpm=120, sr=48000)。"""
    return "%d/%d" % (frames, QUARTER)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def plain_timeline(path):
    testgen.write_plain_timeline(path)


def mix_manifest(path, tracks, sr=SR):
    write_json(path, {"sample_rate": sr, "tracks": tracks})


# ================================================================ T1 时间轴

def t1():
    testgen.write_sample_timeline(os.path.join(ROOT, "sample_timeline.json"))
    segs = [(Fraction(0), Fraction(120)), (Fraction(8), Fraction(165, 2)),
            (Fraction(20), Fraction(99))]
    events = ["1/3", "5/7", "7", "8", "29/3", "19", "25"]
    ok = True
    detail = ""
    for sr in (48000, 44100):
        r = run(["timeline", "--timeline", "sample_timeline.json",
                 "--sample-rate", str(sr)])
        if r.returncode != 0:
            ok, detail = False, "exit %d: %s" % (r.returncode, r.stderr)
            break
        out = json.loads(r.stdout)
        got = {e["name"]: e["sample"] for e in out["events"]}
        for name, pos in zip(["e1", "e2", "e3", "e4", "e5", "e6", "e7"], events):
            truth = truth_samples(segs, Fraction(pos), sr)
            want = truth.numerator // truth.denominator   # 向下取整
            if got.get(name) != want:
                ok = False
                detail = "sr=%d %s: got %s want %d" % (sr, name, got.get(name), want)
                break
    check("T1 有理数时间轴定位(48000/44100 与 Fraction 真值误差 0)", ok, detail)


# ================================================================ T2 流式读写

def t2():
    d = os.path.join(TD, "t2")
    os.makedirs(d, exist_ok=True)
    # 2 声道文件,含 8388607 / -8388608 / 0
    samples = [S24_MAX, S24_MIN, 0, 1, -1, S24_MAX, S24_MIN, 0]
    src = os.path.join(d, "extremes.pcm")
    testgen.write_pcm(src, samples)
    expected = iencode(samples)          # 独立编码器构造的期望字节
    ok = read_bytes(src) == expected
    hexpat = " ".join("%02x" % b for b in expected[:12])
    dst = os.path.join(d, "extremes_out.pcm")
    r = run(["copy", "--input", src, "--output", dst, "--channels", "2",
             "--window-samples", "4"])
    ok = ok and r.returncode == 0 and read_bytes(dst) == expected
    check("T2a 24bit 三字节小端读写(极值 hex=%s)" % hexpat, ok,
          r.stderr if r.returncode else "byte mismatch")

    # ~50MB 与 ~25MB:峰值持有量不随长度增长
    big = os.path.join(d, "big50.pcm")
    small = os.path.join(d, "small25.pcm")
    frames_big = 49999998 // 6           # ~50MB 立体声
    frames_small = frames_big // 2
    testgen.write_pattern_file(big, frames_big, 2)
    testgen.write_pattern_file(small, frames_small, 2)
    peaks = []
    ok2 = True
    detail = ""
    for src2, frames in ((big, frames_big), (small, frames_small)):
        out = src2 + ".out"
        r = run(["copy", "--input", src2, "--output", out, "--channels", "2",
                 "--block-samples", "65536", "--window-samples", "65536",
                 "--trace-peak-window"])
        if r.returncode != 0:
            ok2, detail = False, r.stderr
            break
        rep = last_json(r.stdout)
        peaks.append(rep["peak_held_samples"])
        if rep["peak_held_samples"] > 65536:
            ok2, detail = False, "peak %d > window" % rep["peak_held_samples"]
            break
        if sha_of(out) != sha_of(src2):
            ok2, detail = False, "round-trip sha mismatch"
            break
    ok2 = ok2 and peaks[0] == peaks[1]
    check("T2b ~50MB 流式窗口(peak=%s, 两长度峰值相等且不超窗口)" % peaks,
          ok2, detail)


# ================================================================ T3 多轨混音

def t3():
    d = os.path.join(TD, "t3")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    frames = 1500
    t1data = testgen.sine(frames, SR, 440, 2000000, 1)
    t2data = testgen.impulse(frames, 97, 3000000, 2)
    t3data = testgen.sine(frames, SR, 1330, 1000000, 1)
    testgen.write_pcm(os.path.join(d, "t1.pcm"), t1data)
    testgen.write_pcm(os.path.join(d, "t2.pcm"), t2data)
    testgen.write_pcm(os.path.join(d, "t3.pcm"), t3data)
    tracks = [
        {"id": "t1", "file": "t1.pcm", "channels": 1, "gain": "3/4", "pan": "-1/2",
         "intervals": [{"id": "a", "start": "0", "length": frames}]},
        {"id": "t2", "file": "t2.pcm", "channels": 2, "gain": "2/3", "pan": "1/4",
         "intervals": [{"id": "a", "start": "0", "length": frames}]},
        {"id": "t3", "file": "t3.pcm", "channels": 1, "gain": "1/2", "pan": "1",
         "intervals": [{"id": "a", "start": "0", "length": frames}]},
    ]
    man = os.path.join(d, "m.json")
    mix_manifest(man, tracks)

    # 独立参照:Fraction 定点混音 + 归一化
    def ref():
        mixL = [0] * frames
        mixR = [0] * frames
        for data, ch, gain, pan in (
                (t1data, 1, Fraction(3, 4), Fraction(-1, 2)),
                (t2data, 2, Fraction(2, 3), Fraction(1, 4)),
                (t3data, 1, Fraction(1, 2), Fraction(1))):
            gl = gain * (1 - pan) / 2
            gr = gain * (1 + pan) / 2
            for i in range(frames):
                if ch == 1:
                    v = data[i]
                    mixL[i] += irhalf(v * gl.numerator, gl.denominator)
                    mixR[i] += irhalf(v * gr.numerator, gr.denominator)
                else:
                    mixL[i] += irhalf(data[2 * i] * gl.numerator, gl.denominator)
                    mixR[i] += irhalf(data[2 * i + 1] * gr.numerator, gr.denominator)
        inter = []
        peak = 0
        for i in range(frames):
            l, r = iclamp(mixL[i]), iclamp(mixR[i])
            inter += [l, r]
            peak = max(peak, abs(l), abs(r))
        g = Fraction(S24_MAX, peak) if peak else Fraction(1)
        return iencode([iclamp(irhalf(v * g.numerator, g.denominator)) for v in inter])

    want = ref()
    shas = []
    ok = True
    detail = ""
    for blk in (1, 17, 4096):
        out = os.path.join(d, "out_%d.pcm" % blk)
        r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out,
                 "--block-samples", str(blk)])
        if r.returncode != 0:
            ok, detail = False, "block %d: %s" % (blk, r.stderr)
            break
        shas.append(sha_of(out))
        if read_bytes(out) != want:
            ok, detail = False, "block %d != Fraction reference" % blk
            break
    ok = ok and len(set(shas)) == 1
    check("T3a Fraction 定点混音 == 参照;块 1/17/4096 SHA256 一致", ok, detail)

    # 削波:双轨满幅叠加
    cman = os.path.join(d, "clip.json")
    mix_manifest(cman, [
        {"id": "c1", "channels": 1, "gain": "2", "pan": "0",
         "intervals": [{"id": "a", "start": "0", "length": 64, "constant": S24_MAX}]},
        {"id": "c2", "channels": 1, "gain": "2", "pan": "0",
         "intervals": [{"id": "a", "start": "0", "length": 64, "constant": S24_MAX}]},
        {"id": "c3", "channels": 1, "gain": "2", "pan": "0",
         "intervals": [{"id": "a", "start": q(64), "length": 64, "constant": S24_MIN}]},
        {"id": "c4", "channels": 1, "gain": "2", "pan": "0",
         "intervals": [{"id": "a", "start": q(64), "length": 64, "constant": S24_MIN}]},
    ])
    out = os.path.join(d, "clip_out.pcm")
    r = run(["mix", "--manifest", cman, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    ok2 = (r.returncode == 0 and all(S24_MIN <= v <= S24_MAX for v in vals)
           and max(vals) == S24_MAX and min(vals) == S24_MIN)
    check("T3b 叠加削波饱和到 [-8388608,8388607],正峰恰为 8388607", ok2,
          r.stderr if r.returncode else "min=%d max=%d" % (min(vals), max(vals)))


# ================================================================ T4 间断输入

def t4():
    d = os.path.join(TD, "t4")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    ivs = [
        {"id": "A", "start": q(0), "length": 1000, "constant": 777},
        {"id": "B", "start": q(900), "length": 1000, "constant": 1000},
        {"id": "C", "start": q(2100), "length": 500, "constant": 555},
        {"id": "D", "start": q(2600), "length": 1, "constant": S24_MAX},  # 使归一化增益=1
    ]
    outs = []
    for name, order in (("fwd", ivs), ("rev", list(reversed(ivs)))):
        man = os.path.join(d, "m_%s.json" % name)
        mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                            "intervals": order}])
        out = os.path.join(d, "out_%s.pcm" % name)
        r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
        if r.returncode != 0:
            check("T4 间断输入", False, r.stderr)
            return
        outs.append(out)
    vals = idecode(read_bytes(outs[0]))
    left = vals[0::2]
    overlap_ok = all(v == 1000 for v in left[900:1000])
    gap_ok = all(v == 0 for v in left[1900:2100])
    a_ok = all(v == 777 for v in left[0:900])
    c_ok = all(v == 555 for v in left[2100:2600])
    same = sha_of(outs[0]) == sha_of(outs[1])
    check("T4a 重叠 100 采样点后开始者覆盖(=1000)、间隙 200 采样点静音、"
          "声明顺序颠倒 SHA256 不变",
          overlap_ok and gap_ok and a_ok and c_ok and same,
          "overlap=%s gap=%s A=%s C=%s same=%s" % (overlap_ok, gap_ok, a_ok, c_ok, same))

    # 同起点 tie-break:声明顺序后者覆盖前者
    man = os.path.join(d, "tie.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                        "intervals": [
                            {"id": "x", "start": "0", "length": 10, "constant": 111},
                            {"id": "y", "start": "0", "length": 10, "constant": S24_MAX},
                        ]}])
    out = os.path.join(d, "tie_out.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    check("T4b 同起点时声明顺序后者覆盖前者", r.returncode == 0
          and all(v == S24_MAX for v in vals[0:20:2]),
          r.stderr if r.returncode else "")


# ================================================================ T5 原子提交

def t5():
    d = os.path.join(TD, "t5")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 2000,
                                       "constant": 123456}]}])
    target = os.path.join(d, "master.pcm")
    with open(target, "wb") as f:
        f.write(b"PLACEHOLDER")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", target])
    ok = r.returncode == 0 and read_bytes(target) != b"PLACEHOLDER"
    rep = last_json(r.stdout) if r.returncode == 0 else {}
    ok = ok and rep.get("sha256") == sha_of(target)
    check("T5a 正常运行后占位目标被新母带原子替换", ok, r.stderr)

    # 注入受控 I/O 失败
    with open(target, "wb") as f:
        f.write(b"PLACEHOLDER")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", target,
             "--fail-after-output-bytes", "100"])
    leftover = [f for f in os.listdir(d)
                if f.endswith((".tmp1", ".tmp2", ".recover.json", ".tmp"))]
    err = {}
    try:
        err = json.loads(r.stderr.strip())
    except ValueError:
        pass
    ok = (r.returncode == 1 and err.get("code") == "E_COMMIT_FAILED"
          and read_bytes(target) == b"PLACEHOLDER" and not leftover)
    check("T5b 注入失败:目标不变/无临时残留/退出码1/stderr 含 E_COMMIT_FAILED",
          ok, "rc=%d err=%s leftover=%s" % (r.returncode, r.stderr.strip(), leftover))


# ================================================================ T6 崩溃恢复

def t6():
    d = os.path.join(TD, "t6")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    frames = 20000
    testgen.write_pcm(os.path.join(d, "t.pcm"),
                      testgen.sine(frames, SR, 220, 1500000, 1))
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "file": "t.pcm", "channels": 1,
                        "gain": "3/2", "pan": "-1/4",
                        "intervals": [{"id": "a", "start": "0", "length": frames}]}])
    ref = os.path.join(d, "ref.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", ref])
    if r.returncode != 0:
        check("T6 崩溃恢复", False, r.stderr)
        return
    ref_sha = sha_of(ref)

    out = os.path.join(d, "crash.pcm")
    logpath = out + ".recover.json"
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out,
             "--crash-after-output-samples", "5000"])
    crashed = r.returncode != 0 and os.path.exists(logpath)
    log_ok = False
    if crashed:
        try:
            log = json.loads(read_bytes(logpath).decode())
            log_ok = (log["output_samples"] > 0
                      and log["output_bytes"] == log["output_samples"] * 6
                      and log["phase"] in (1, 2) and log["peak"] >= 0)
        except (ValueError, KeyError):
            pass
    check("T6a 强制终止后恢复日志含合法偏移与采样数(phase=%s)"
          % (log.get("phase") if crashed else "?"), crashed and log_ok,
          "rc=%d" % r.returncode)

    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out,
             "--crash-after-output-samples", "5000"])
    ok = (r.returncode == 0 and sha_of(out) == ref_sha
          and not os.path.exists(logpath))
    check("T6b 重启同一命令:输出与未中断运行 SHA256 一致且日志已删除", ok,
          "rc=%d" % r.returncode)

    # 日志偏移改为负数 -> 告警 + 从头重算,退出码 0
    out2 = os.path.join(d, "crash2.pcm")
    log2 = out2 + ".recover.json"
    run(["mix", "--manifest", man, "--timeline", tl, "--output", out2,
         "--crash-after-output-samples", "5000"])
    log = json.loads(read_bytes(log2).decode())
    log["output_samples"] = -5
    log["output_bytes"] = -30
    with open(log2, "w", encoding="utf-8") as f:
        json.dump(log, f)
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out2,
             "--crash-after-output-samples", "5000"])
    rep = last_json(r.stdout) if r.returncode == 0 else {}
    warn_codes = [w.get("code") for w in rep.get("warnings", [])]
    ok = (r.returncode == 0 and "E_LOG_CORRUPT" in warn_codes
          and sha_of(out2) == ref_sha and not os.path.exists(log2))
    check("T6c 日志偏移非法:E_LOG_CORRUPT 告警+从头重算,退出码 0 且输出一致",
          ok, "rc=%d warnings=%s" % (r.returncode, warn_codes))


# ================================================================ T7 错误协议

def t7():
    d = os.path.join(TD, "t7")
    os.makedirs(d, exist_ok=True)
    results = {}

    bad_tl = os.path.join(d, "bpm0.json")
    write_json(bad_tl, {"segments": [{"start": "0", "bpm": "0"}], "events": []})
    r = run(["timeline", "--timeline", bad_tl, "--sample-rate", "48000"])
    results["E_TIMELINE"] = r

    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    man = os.path.join(d, "range.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 10,
                                       "constant": 10000000}]}])
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output",
             os.path.join(d, "o1.pcm")])
    results["E_SAMPLE_RANGE"] = r

    man = os.path.join(d, "missing.json")
    mix_manifest(man, [{"id": "t", "file": "no_such_file.pcm", "channels": 1,
                        "intervals": [{"id": "a", "start": "0", "length": 10}]}])
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output",
             os.path.join(d, "o2.pcm")])
    results["E_MISSING_INPUT"] = r

    man = os.path.join(d, "ok.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 100,
                                       "constant": 7}]}])
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output",
             os.path.join(d, "o3.pcm"), "--fail-after-output-bytes", "10"])
    results["E_COMMIT_FAILED"] = r

    r = run(["frobnicate"])
    results["E_USAGE"] = r

    allok = True
    for code, r in results.items():
        try:
            err = json.loads(r.stderr.strip())
        except ValueError:
            err = {}
        single_line = len(r.stderr.strip().splitlines()) == 1
        want_rc = 2 if code == "E_USAGE" else 1
        good = (err.get("code") == code and r.returncode == want_rc and single_line)
        allok = allok and good
        if not good:
            print("    %s: rc=%d stderr=%r" % (code, r.returncode, r.stderr.strip()))
    check("T7 错误协议(E_TIMELINE/E_SAMPLE_RANGE/E_MISSING_INPUT/"
          "E_COMMIT_FAILED 退出码1,E_USAGE 退出码2,单行 JSON)", allok)


# ================================================================ T8 分块与窗口

def t8():
    d = os.path.join(TD, "t8")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    frames = 100000
    testgen.write_pcm(os.path.join(d, "t.pcm"),
                      testgen.sine(frames, SR, 100, 2000000, 1))
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "file": "t.pcm", "channels": 1,
                        "gain": "2", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": frames}]}])
    out_w = os.path.join(d, "w.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out_w,
             "--block-samples", "4096", "--window-samples", "512",
             "--trace-window", "--trace-peak-window"])
    if r.returncode != 0:
        check("T8 窗口约束", False, r.stderr)
        return
    traces = [json.loads(l) for l in r.stdout.strip().splitlines()
              if '"trace_window"' in l]
    held_ok = all(t["held_peak"] <= 512 for t in traces)
    # 已处理块的输入读取范围不再出现(单调前进、无重叠)
    seen_end = 0
    range_ok = True
    for t in traces:
        for rd in t["reads"]:
            if "file_frame" in rd:
                if rd["file_frame"] < seen_end:
                    range_ok = False
                seen_end = max(seen_end, rd["file_frame"] + rd["frames"])
    rep = last_json(r.stdout)
    out_n = os.path.join(d, "n.pcm")
    r2 = run(["mix", "--manifest", man, "--timeline", tl, "--output", out_n,
              "--block-samples", "4096"])
    same = r2.returncode == 0 and sha_of(out_w) == sha_of(out_n)
    check("T8 window=512 处理 100000 帧:持有量<=512、已处理范围不重现、"
          "与无窗口 SHA256 一致(peak=%d)" % rep["peak_held_samples"],
          held_ok and range_ok and same,
          "held_ok=%s range_ok=%s same=%s" % (held_ok, range_ok, same))


# ================================================================ T9 分数采样偏移

def t9():
    d = os.path.join(TD, "t9")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                        "intervals": [
                            {"id": "i1", "start": q(10), "offset": "1/3",
                             "length": 4, "constant": S24_MAX},
                            {"id": "i2", "start": q(20), "offset": "2/5",
                             "length": 4, "constant": 12345},
                            {"id": "i3", "start": q(30), "offset": "3/5",
                             "length": 4, "constant": 22222},
                        ]}])
    r = run(["positions", "--manifest", man, "--timeline", tl])
    ok = r.returncode == 0
    detail = ""
    if ok:
        rows = {x["interval"]: x for x in json.loads(r.stdout)["intervals"]}
        truth = {"i1": Fraction(10) + Fraction(1, 3),
                 "i2": Fraction(20) + Fraction(2, 5),
                 "i3": Fraction(30) + Fraction(3, 5)}
        for k, f in truth.items():
            if rows[k]["position_exact"] != str(f):
                ok, detail = False, "%s exact %s != %s" % (k, rows[k]["position_exact"], f)
            want = irhalf(f.numerator, f.denominator)
            if rows[k]["sample"] != want:
                ok, detail = False, "%s sample %d != %d" % (k, rows[k]["sample"], want)
    out = os.path.join(d, "o.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    left = vals[0::2]
    place_ok = (all(v == S24_MAX for v in left[10:14])
                and all(v == 12345 for v in left[20:24])
                and all(v == 22222 for v in left[31:35])
                and left[30] == 0)
    check("T9a 偏移 1/3、2/5(及 3/5)采样点:位置与 Fraction 四舍五入真值一致",
          ok and r.returncode == 0 and place_ok, detail or "placement wrong")

    # 100 个区间,每段增量偏移 1/7(清单显式声明 delta)
    ivs = [{"id": "d%d" % k, "start": "0", "offset": "1/7",
            "offset_mode": "delta", "length": 1, "constant": 0}
           for k in range(100)]
    man = os.path.join(d, "delta.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": ivs}])
    r = run(["positions", "--manifest", man, "--timeline", tl])
    rows = {x["interval"]: x for x in json.loads(r.stdout)["intervals"]}
    acc = sum(Fraction(1, 7) for _ in range(100))   # 直接 Fraction 累加
    last = rows["d99"]
    ok = (r.returncode == 0 and last["position_exact"] == str(acc)
          and last["sample"] == irhalf(acc.numerator, acc.denominator))
    check("T9b 连续 100 区间每段偏移 1/7:末区间绝对位置 == 直接累加(%s),误差 0"
          % acc, ok, "got %s" % last)


# ================================================================ T10 峰值归一化

def t10():
    d = os.path.join(TD, "t10")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    data = testgen.sine(2000, SR, 500, 3000, 1)
    data[100] = 4194303        # 正峰 = 全局最大绝对值
    data[200] = -4194300
    testgen.write_pcm(os.path.join(d, "t.pcm"), data)
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "file": "t.pcm", "channels": 1,
                        "gain": "2", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 2000}]}])
    out = os.path.join(d, "o.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    ok = (r.returncode == 0 and max(vals) == S24_MAX
          and all(S24_MIN <= v <= S24_MAX for v in vals))
    check("T10a 峰值<=4194303 的混音归一化后正峰恰为 8388607 且全域合法", ok,
          "max=%d min=%d" % (max(vals), min(vals)) if vals else r.stderr)

    man = os.path.join(d, "zero.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 500,
                                       "constant": 0}]}])
    out = os.path.join(d, "z.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    rep = last_json(r.stdout)
    ok = (r.returncode == 0 and all(v == 0 for v in vals)
          and rep["peak"] == 0 and rep["normalize_gain"] == "1")
    check("T10b 全零输入输出全零且不除零", ok, r.stderr)

    man = os.path.join(d, "gap.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                        "intervals": [
                            {"id": "a", "start": q(0), "length": 100, "constant": 1000},
                            {"id": "b", "start": q(200), "length": 100,
                             "constant": -1000}]}])
    out = os.path.join(d, "g.pcm")
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    vals = idecode(read_bytes(out))
    left = vals[0::2]
    ok = (r.returncode == 0 and all(v == 0 for v in left[100:200])
          and left[0] == S24_MAX and left[200] == -S24_MAX)
    check("T10c 含全零区间的输入在相应位置保持全零", ok,
          r.stderr if r.returncode else "")


# ================================================================ T11 validate

def t11():
    d = os.path.join(TD, "t11")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    plain_timeline(tl)
    man = os.path.join(d, "m.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "2", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 640,
                                       "constant": 100000}]}])
    master = os.path.join(d, "m.pcm")
    sidecar = master + ".sidecar.json"
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", master,
             "--block-samples", "64"])
    if r.returncode != 0:
        check("T11 validate", False, r.stderr)
        return
    r = run(["validate", "--master", master, "--sidecar", sidecar])
    rep_clean = last_json(r.stdout)
    ok_clean = r.returncode == 0 and rep_clean["ok"] and rep_clean["mismatches"] == []

    # 改第 3 块一个字节
    buf = bytearray(read_bytes(master))
    buf[3 * 64 * 6 + 5] ^= 0xFF
    with open(master, "wb") as f:
        f.write(buf)
    r = run(["validate", "--master", master, "--sidecar", sidecar])
    rep_mm = last_json(r.stdout)
    ok_mm = (r.returncode == 1 and not rep_mm["ok"] and rep_mm["mismatches"] == [3])
    check("T11a validate:全部一致退出码 0;改块后报告不一致块索引 [3] 且退出码 1",
          ok_clean and ok_mm, "clean=%s mm=%s" % (rep_clean, rep_mm))

    # 断点续算
    logpath = master + ".validate.recover.json"
    r = run(["validate", "--master", master, "--sidecar", sidecar,
             "--crash-after-blocks", "2"])
    crashed = r.returncode != 0 and os.path.exists(logpath)
    r = run(["validate", "--master", master, "--sidecar", sidecar,
             "--crash-after-blocks", "2"])
    rep_resume = last_json(r.stdout) if r.stdout.strip() else {}
    ok_resume = (crashed and r.returncode == 1
                 and rep_resume.get("mismatches") == rep_mm["mismatches"]
                 and not os.path.exists(logpath))
    check("T11b --crash-after-blocks 断点续算:最终报告与未中断一致", ok_resume,
          "crashed=%s rep=%s" % (crashed, rep_resume))


# ================================================================ T12 事件一致性

def t12():
    d = os.path.join(TD, "t12")
    os.makedirs(d, exist_ok=True)
    tl = os.path.join(d, "tl.json")
    write_json(tl, {
        "segments": [{"start": "0", "bpm": "120"}],
        "events": [
            {"name": "ev1", "position": "0", "track": "t", "interval": "a"},
            {"name": "ev2", "position": "1", "track": "t", "interval": "a"},
        ]})
    man = os.path.join(d, "bad.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 1000,
                                       "constant": 5}]}])
    out = os.path.join(d, "o.pcm")
    before = set(os.listdir(d))
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    err = {}
    try:
        err = json.loads(r.stderr.strip())
    except ValueError:
        pass
    new_files = set(os.listdir(d)) - before
    no_leftover = not any(f.endswith((".tmp1", ".tmp2", ".recover.json"))
                          or f == "o.pcm" for f in new_files)
    ok = r.returncode == 1 and err.get("code") == "E_EVENT_MISMATCH" and no_leftover
    check("T12a 事件超出区间时长:E_EVENT_MISMATCH、退出码 1、无临时/最终文件",
          ok, "rc=%d err=%s new=%s" % (r.returncode, err, new_files))

    man = os.path.join(d, "good.json")
    mix_manifest(man, [{"id": "t", "channels": 1, "gain": "1", "pan": "0",
                        "intervals": [{"id": "a", "start": "0", "length": 25000,
                                       "constant": 5}]}])
    r = run(["mix", "--manifest", man, "--timeline", tl, "--output", out])
    check("T12b 修正区间时长后同命令成功,退出码 0", r.returncode == 0, r.stderr)


def main():
    if os.path.isdir(TD):
        shutil.rmtree(TD)
    os.makedirs(TD)
    testgen.main()
    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12):
        fn()
    failed = [n for n, ok in RESULTS if not ok]
    print("\n==== %d/%d checks passed ====" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("FAILED: " + "; ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
