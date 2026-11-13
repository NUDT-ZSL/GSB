#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify — 对 wavconcat.py 执行 14 条验收检查，全部通过才以 0 退出。"""
import os
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "wavconcat.py")
SAMPLES = os.path.join(HERE, "samples")
OUT = os.path.join(HERE, "out")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" — " + detail) if detail else ""))
    return ok


def run(manifest_name, out_name, extra_manifest_dir=None):
    manifest = os.path.join(extra_manifest_dir or SAMPLES, manifest_name)
    out = os.path.join(OUT, out_name)
    if os.path.exists(out):
        os.unlink(out)
    p = subprocess.run([sys.executable, TOOL, manifest, out],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr, out


def read_wav(path):
    with wave.open(path, "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate(),
                  w.getnframes())
        raw = w.readframes(w.getnframes())
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw)
    return params, raw, samples


def leftover_temps():
    if not os.path.isdir(OUT):
        return []
    return [f for f in os.listdir(OUT) if f.startswith(".wavconcat-")]


def main():
    os.makedirs(OUT, exist_ok=True)
    # 确保样本存在
    subprocess.run([sys.executable, os.path.join(HERE, "make_samples.py")],
                   check=True, capture_output=True)

    # 1. 仅接受 16 位/单声道/8000..48000Hz
    rc, _, err, out = run("stereo.json", "t1a.wav")
    check("1a 立体声拒绝", rc != 0 and "格式不支持" in err
          and not os.path.exists(out), err.strip())
    rc, _, err, out = run("wav24.json", "t1b.wav")
    check("1b 24位拒绝", rc != 0 and "格式不支持" in err and "16" in err
          and not os.path.exists(out), err.strip())
    rc, _, err, out = run("rate_6000.json", "t1c.wav")
    check("1c 采样率6000拒绝", rc != 0 and "6000" in err
          and not os.path.exists(out), err.strip())

    # 2. 缺 path 指明条目
    rc, _, err, out = run("missing_path.json", "t2.wav")
    check("2 缺path报错并指明索引", rc != 0 and "files[1]" in err
          and "path" in err and not os.path.exists(out), err.strip())

    # 3. 采样率不一致，错误含两个采样率
    rc, _, err, out = run("mixed_rate.json", "t3.wav")
    check("3 混合采样率拒绝", rc != 0 and "44100" in err and "48000" in err
          and not os.path.exists(out), err.strip())

    # 4. fade_ms 越界/非整数，指明索引
    rc, _, err, out = run("fade_600.json", "t4a.wav")
    check("4a fade_ms=600拒绝", rc != 0 and "files[0]" in err
          and not os.path.exists(out), err.strip())
    rc, _, err, out = run("fade_float.json", "t4b.wav")
    check("4b fade_ms=1.5拒绝", rc != 0 and "files[1]" in err
          and not os.path.exists(out), err.strip())

    # 5. 淡变帧数超片段一半
    rc, _, err, out = run("long_fade.json", "t5.wav")
    check("5 帧数不足拒绝", rc != 0 and "帧数不足" in err
          and not os.path.exists(out), err.strip())

    # 6 + 14. 正常拼接：帧数等于和，摘要数值与 wave 读取一致
    rc, stdout, err, out = run("manifest.json", "t6.wav")
    ok = rc == 0 and os.path.exists(out)
    if ok:
        (ch, sw, rate, n), _, _ = read_wav(out)
        ok = (n == 4000 + 3000 + 5000 and rate == 44100 and ch == 1
              and sw == 2 and "片段数: 3" in stdout
              and "总帧数: 12000" in stdout and "采样率: 44100" in stdout
              and "时长: 0.272" in stdout)
    check("6+14 拼接帧数与摘要", ok,
          "rc=%d stdout=%r" % (rc, stdout.strip()))

    # 7. 单片段逐样本一致
    rc, _, _, out = run("single.json", "t7.wav")
    ok = rc == 0
    if ok:
        _, raw_in, _ = read_wav(os.path.join(SAMPLES, "single.wav"))
        _, raw_out, _ = read_wav(out)
        ok = raw_in == raw_out
    check("7 单片段逐样本一致", ok)

    # 8. 线性整数增益序列（恒定幅度 12000，fade 10ms @44100 = 441 帧）
    rc, _, _, out = run("linear.json", "t8.wav")
    ok = rc == 0
    detail = ""
    if ok:
        _, _, s = read_wav(out)
        nf = 441
        expect_tail = [12000 * (nf - k) // nf for k in range(nf)]
        expect_head = [12000 * k // nf for k in range(nf)]
        got_tail = list(s[2000 - nf:2000])
        got_head = list(s[2000:2000 + nf])
        ok = got_tail == expect_tail and got_head == expect_head
        detail = "tail[0]=%d tail[-1]=%d head[0]=%d head[-1]=%d" % (
            got_tail[0], got_tail[-1], got_head[0], got_head[-1])
    check("8 接缝线性增益序列", ok, detail)

    # 9. 满幅样本：无符号翻转、无超幅（含单元级饱和检查）
    sys.path.insert(0, HERE)
    import wavconcat
    unit_ok = (wavconcat.scale_sample(32767, 1, 1) == 32767
               and wavconcat.scale_sample(-32768, 1, 1) == -32768
               and wavconcat.scale_sample(32767, 3, 1) == 32767   # 饱和
               and wavconcat.scale_sample(-32768, 3, 1) == -32768
               and wavconcat.scale_sample(32767, 1, 441) >= 0
               and wavconcat.scale_sample(-32768, 1, 441) <= 0)
    rc, _, _, out = run("full.json", "t9.wav")
    ok = rc == 0 and unit_ok
    if rc == 0:
        _, _, s = read_wav(out)
        nf = 441
        tail = s[1000 - nf:1000]      # 前段淡出区
        head = s[1000:1000 + nf]      # 后段淡入区
        signs_ok = all((v >= 0) == ((1000 - nf + i) % 2 == 0)
                       for i, v in enumerate(tail) if v != 0)
        signs_ok = signs_ok and all(
            (v >= 0) == ((1000 + i) % 2 == 0)
            for i, v in enumerate(head) if v != 0)
        ok = ok and signs_ok and max(s) <= 32767 and min(s) >= -32768
    check("9 满幅无符号翻转/饱和", ok)

    # 10. 路径越界拒绝
    rc, _, err, out = run("escape.json", "t10.wav")
    check("10 路径越界拒绝", rc != 0 and "越界" in err
          and not os.path.exists(out), err.strip())

    # 11. 缺失/损坏片段整体失败且无输出
    rc, _, err, out = run("missing_file.json", "t11a.wav")
    ok1 = rc != 0 and not os.path.exists(out)
    rc2, _, err2, out2 = run("corrupt.json", "t11b.wav")
    ok2 = rc2 != 0 and not os.path.exists(out2)
    check("11 缺失/损坏片段无输出", ok1 and ok2,
          (err.strip() + " | " + err2.strip()).strip())

    # 12. 预置旧输出，中途失败后旧文件字节不变
    old_bytes = b"OLD-OUTPUT-BYTES-" * 64
    out12 = os.path.join(OUT, "t12.wav")
    with open(out12, "wb") as f:
        f.write(old_bytes)
    p = subprocess.run([sys.executable, TOOL,
                        os.path.join(SAMPLES, "mixed_rate.json"), out12],
                       capture_output=True, text=True)
    with open(out12, "rb") as f:
        after = f.read()
    check("12 失败时旧输出不变",
          p.returncode != 0 and after == old_bytes
          and not leftover_temps())

    # 13. 空清单与 0 帧片段
    rc, _, err, out = run("empty.json", "t13a.wav")
    ok1 = rc != 0 and not os.path.exists(out)
    rc2, _, err2, out2 = run("zero_frame.json", "t13b.wav")
    ok2 = rc2 != 0 and not os.path.exists(out2)
    check("13 空清单/0帧片段拒绝", ok1 and ok2,
          (err.strip() + " | " + err2.strip()).strip())

    # 收尾：确认没有残留临时文件
    check("收尾 无残留临时文件", not leftover_temps())

    n_fail = sum(1 for _, ok, _ in RESULTS if not ok)
    print("\n%d 项检查，%d 通过，%d 失败"
          % (len(RESULTS), len(RESULTS) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
