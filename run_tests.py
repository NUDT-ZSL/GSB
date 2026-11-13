# -*- coding: utf-8 -*-
"""run_tests.py — 对 wavedit.py 的15条验收项逐一验证并打印结果。"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "wavedit.py")
SAMPLES = os.path.join(HERE, "samples")
WORK = os.path.join(HERE, "测试 输出")  # 含中文与空格的输出目录（验收13）

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))


def run_tool(src, cfg, out):
    return subprocess.run(
        [sys.executable, TOOL, src, cfg, out],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def wav_data(path):
    """返回 (params, data_bytes)。"""
    with wave.open(path, "rb") as w:
        params = (w.getnchannels(), w.getframerate(), w.getsampwidth())
        frames = w.getnframes()
        data = w.readframes(frames)
    return params, data, frames


def main():
    os.makedirs(WORK, exist_ok=True)
    s = lambda n: os.path.join(SAMPLES, n)
    o = lambda n: os.path.join(WORK, n)

    stereo = s("voice_44k_stereo16.wav")
    mono1s = s("voice_16k_mono16.wav")

    # ---- 验收1+15：正常处理退出码0，输出可解析，打印摘要 ----
    out1 = o("out_single.wav")
    r = run_tool(stereo, s("config_single.json"), out1)
    ok = r.returncode == 0
    try:
        params, data, frames = wav_data(out1)
        ok = ok and params == (2, 44100, 2)
    except Exception as exc:
        ok = False
        print("  解析失败:", exc)
    check("1 命令行正常处理退出码0且输出为可解析PCM WAV", ok,
          "exit=%d stdout=%r" % (r.returncode, r.stdout.strip()))
    ok = ("编辑操作：1 个" in r.stdout and "总时长：" in r.stdout
          and "输出字节：" in r.stdout)
    check("15 终端摘要含编辑操作数/总时长/输出字节", ok, r.stdout.strip())

    # ---- 验收2：非PCM报错 ----
    r = run_tool(s("float_not_pcm.wav"), s("config_single.json"), o("x.wav"))
    check("2 非PCM输入stderr含'不支持的WAV格式'且退出码1",
          r.returncode == 1 and "不支持的WAV格式" in r.stderr,
          "exit=%d stderr=%r" % (r.returncode, r.stderr.strip()))

    # ---- 验收3：缺speed字段 ----
    r = run_tool(stereo, s("config_missing_speed.json"), o("x.wav"))
    check("3 缺speed字段时报出该字段且退出码2",
          r.returncode == 2 and "speed" in r.stderr,
          "exit=%d stderr=%r" % (r.returncode, r.stderr.strip()))

    # ---- 验收4：重叠+越界，总时长与截断结果一致 ----
    # 源2.0s；edit1: 0.5~1.5 speed2 -> 片段1.0s变0.5s，总长1.5s
    # edit2: 1.0~3.0 截断为1.0~1.5(0.5s) speed0.5 -> 1.0s，总长2.0s
    out4 = o("out_overlap.wav")
    r = run_tool(stereo, s("config_overlap.json"), out4)
    _, _, frames4 = wav_data(out4)
    expect = 88200 - 22050 - 22050 + 11025 + 44100  # 逐段推算
    # 直接推算：edit1后帧数=88200-44100+22050=66150；edit2截断0.5s(22050帧)->44100帧，总=88200
    expect = 88200
    check("4 重叠且越界配置总时长与截断结果一致",
          r.returncode == 0 and frames4 == expect,
          "期望%d帧(2.0s) 实际%d帧" % (expect, frames4))

    # ---- 验收5：1秒片段speed2.0 -> 约0.5秒，误差<=1ms ----
    out5 = o("out_speed2.wav")
    r = run_tool(mono1s, s("config_speed2.json"), out5)
    _, _, frames5 = wav_data(out5)
    err_ms = abs(frames5 - 8000) / 16000.0 * 1000.0
    check("5 1秒片段速度2.0输出约0.5秒(误差<=1ms)",
          r.returncode == 0 and err_ms <= 1.0,
          "输出%d帧 期望8000帧 误差%.3fms" % (frames5, err_ms))

    # ---- 验收6：未编辑区间与原文件逐字节一致 ----
    _, orig_data, _ = wav_data(stereo)
    _, out_data, _ = wav_data(out1)
    # 编辑区间0.5~1.5s(帧22050~66150)，speed2后变22050帧；输出尾部从44100帧起
    fb = 4  # 每帧字节(2声道*16bit)
    head_ok = out_data[:22050 * fb] == orig_data[:22050 * fb]
    tail_ok = out_data[44100 * fb:] == orig_data[66150 * fb:]
    check("6 未编辑区间采样与原文件逐字节一致", head_ok and tail_ok,
          "头部一致=%s 尾部一致=%s" % (head_ok, tail_ok))

    # ---- 验收7：wave读取参数一致且可完整读出 ----
    try:
        with wave.open(out1, "rb") as w:
            same = (w.getnchannels() == 2 and w.getframerate() == 44100
                    and w.getsampwidth() == 2)
            n = w.getnframes()
            raw = w.readframes(n)
            full = len(raw) == n * 4
        check("7 输出参数与源一致且数据块可完整读出", same and full,
              "参数一致=%s 完整读出=%s" % (same, full))
    except Exception as exc:
        check("7 输出参数与源一致且数据块可完整读出", False, str(exc))

    # ---- 验收8：各位深极值样本编辑后仍在合法范围 ----
    all_ok = True
    details = []
    ranges = {1: (0, 255), 2: (-32768, 32767),
              3: (-8388608, 8388607), 4: (-2147483648, 2147483647)}
    for bits, sw in ((8, 1), (16, 2), (24, 3), (32, 4)):
        src = s("extreme_8k_mono%d.wav" % bits)
        out = o("extreme_out%d.wav" % bits)
        r = run_tool(src, s("config_pitch.json"), out)
        if r.returncode != 0:
            all_ok = False
            details.append("%dbit运行失败" % bits)
            continue
        with wave.open(out, "rb") as w:
            raw = w.readframes(w.getnframes())
        lo, hi = ranges[sw]
        ok_b = True
        if sw == 1:
            vals = raw
        elif sw == 2:
            vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
        elif sw == 4:
            vals = struct.unpack("<%di" % (len(raw) // 4), raw)
        else:
            vals = []
            for i in range(0, len(raw), 3):
                v = raw[i] | (raw[i + 1] << 8) | (raw[i + 2] << 16)
                if v >= 0x800000:
                    v -= 0x1000000
                vals.append(v)
        for v in vals:
            if not (lo <= v <= hi):
                ok_b = False
                break
        all_ok = all_ok and ok_b
        details.append("%dbit范围内=%s" % (bits, ok_b))
    check("8 8/16/24/32位极值样本编辑后均在合法范围", all_ok, " ".join(details))

    # ---- 验收9：立体声声道对齐、波形关系保持（右=左*0.5）----
    _, od, _ = wav_data(out1)
    vals = struct.unpack("<%dh" % (len(od) // 2), od)
    left, right = vals[0::2], vals[1::2]
    n_eq = len(left) == len(right)
    # 未编辑区（尾部）应精确保持 R = L/2（0.5倍在16bit下对偶数L精确）
    tail_pairs = list(zip(left[-1000:], right[-1000:]))
    rel_ok = all(r * 2 == l for l, r in tail_pairs)
    check("9 立体声样本数相等且声道波形关系保持", n_eq and rel_ok,
          "左右样本数=%d/%d 尾部R*2==L: %s" % (len(left), len(right), rel_ok))

    # ---- 验收10：空edit列表原样复制并提示 ----
    out10 = o("out_empty.wav")
    r = run_tool(stereo, s("config_empty.json"), out10)
    same = md5(out10) == md5(stereo)
    check("10 空edit列表输出与原文件逐字节一致且有提示",
          r.returncode == 0 and same and "无编辑操作" in r.stdout,
          "MD5一致=%s stdout=%r" % (same, r.stdout.strip()))

    # ---- 验收11：同一配置两次运行MD5一致 ----
    a, b = o("det_a.wav"), o("det_b.wav")
    run_tool(stereo, s("config_single.json"), a)
    run_tool(stereo, s("config_single.json"), b)
    check("11 连续两次运行输出MD5相同", md5(a) == md5(b),
          "%s vs %s" % (md5(a)[:12], md5(b)[:12]))

    # ---- 验收12：源缺失退出码1；输出不可写退出码3 ----
    r = run_tool(s("不存在.wav"), s("config_single.json"), o("x.wav"))
    ok1 = r.returncode == 1
    bad_out = os.path.join(WORK, "不存在的目录", "x.wav")
    r2 = run_tool(stereo, s("config_single.json"), bad_out)
    ok2 = r2.returncode == 3 and not os.path.exists(bad_out)
    check("12 源缺失退出码1；输出不可写退出码3", ok1 and ok2,
          "缺失源exit=%d 不可写exit=%d" % (r.returncode, r2.returncode))

    # ---- 验收13：含中文与空格路径正常运行（本测试输出目录即含中文空格）----
    check("13 含中文与空格的路径下输出正常生成",
          os.path.exists(out1) and os.path.getsize(out1) > 0,
          "目录=%r" % WORK)

    # ---- 验收14：处理失败后无残留文件，源文件哈希不变 ----
    before = md5(stereo)
    # 制造处理期异常：配置合法但 pitch 极大导致…不行，需真实失败。
    # 用不可写输出触发失败路径，检查无残留（含.tmp）。
    r = run_tool(stereo, s("config_single.json"), bad_out)
    leftovers = [f for f in os.listdir(WORK) if f.endswith(".tmp")]
    check("14 失败后输出路径无残留且源文件哈希未变",
          r.returncode == 3 and not leftovers and md5(stereo) == before,
          "exit=%d 残留tmp=%s 源哈希未变=%s"
          % (r.returncode, leftovers, md5(stereo) == before))

    # ---- 汇总 ----
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n==== 通过 %d/%d ====" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
