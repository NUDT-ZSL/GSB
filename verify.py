#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""podcast_prep 自动化验收脚本：逐条验证任务要求 1-15。

用法: python verify.py
会先重新生成确定性样本，再运行工具并断言各项验收标准。
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "podcast_prep.py")
GEN = os.path.join(HERE, "make_samples.py")
WORK = os.path.join(HERE, "verify_workspace")
SAMPLES = os.path.join(WORK, "samples")
OUT = os.path.join(WORK, "out")
RATE = 44100

passed = []
failed = []


def check(req, cond, detail=""):
    (passed if cond else failed).append(req)
    print("[%s] 要求%s %s" % ("PASS" if cond else "FAIL", req,
                             (" - " + detail) if detail else ""))


def run_tool(args, **kw):
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True, **kw)


def read_wav(path):
    with wave.open(path, "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate(),
                  w.getnframes())
        raw = w.readframes(w.getnframes())
    samples = struct.unpack("<%dh" % params[3], raw)
    return params, samples


def fresh_dir(d):
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)


def main():
    fresh_dir(WORK)
    subprocess.run([sys.executable, GEN, SAMPLES], check=True,
                   capture_output=True)

    # ---------- 基础运行（要求 1,2,4,5,6,8,9,14） ----------
    r = run_tool([SAMPLES, "-o", OUT])
    report = json.load(open(os.path.join(OUT, "report.json"),
                            encoding="utf-8"))
    files = {e["name"]: e for e in report["files"]}

    # 要求1: 只处理合规文件，其余按名写明跳过原因
    c1 = (files["ep01_ok.wav"]["status"] == "processed"
          and files["ep02_stereo.wav"]["status"] == "skipped"
          and "单声道" in files["ep02_stereo.wav"]["skip_reason"]
          and files["ep03_8bit.wav"]["status"] == "skipped"
          and "16bit" in files["ep03_8bit.wav"]["skip_reason"]
          and files["ep07_48k.wav"]["status"] == "skipped"
          and "44100" in files["ep07_48k.wav"]["skip_reason"])
    check(1, c1, "双声道/8bit/48kHz 均被跳过且原因正确")

    # 要求2: 2s 与 1s 静音各一段，起止误差 <=1ms
    iv = files["ep01_ok.wav"]["silence_intervals"]
    c2 = (len(iv) == 2
          and abs(iv[0]["start_ms"] - 1000) <= 1
          and abs(iv[0]["duration_ms"] - 2000) <= 1
          and abs(iv[1]["start_ms"] - 4000) <= 1
          and abs(iv[1]["duration_ms"] - 1000) <= 1)
    check(2, c2, "实际区间: %s" % iv)

    # 要求4: 全静音片段区间起点为0且覆盖到末尾
    iv5 = files["ep05_allsilence.wav"]["silence_intervals"]
    c4 = (len(iv5) == 1 and iv5[0]["start_ms"] == 0
          and abs(iv5[0]["duration_ms"] - 3000) <= 1)
    check(4, c4, "全静音区间: %s" % iv5)

    # 要求5: 首尾0.2s单调平滑过渡，中段逐采样相同
    _, sin = read_wav(os.path.join(SAMPLES, "ep06_fullscale.wav"))
    p, sout = read_wav(os.path.join(OUT, "ep06_fullscale.wav"))
    f = int(0.2 * RATE)
    n = len(sin)
    head = sout[:f]
    tail = sout[n - f:]
    mid_in, mid_out = sin[f:n - f], sout[f:n - f]
    c5 = (all(head[i] <= head[i + 1] for i in range(f - 1))
          and all(tail[i] >= tail[i + 1] for i in range(f - 1))
          and head[0] == 0 and tail[-1] == 0
          and list(mid_in) == list(mid_out))
    check(5, c5, "头部单调升/尾部单调降/边界为0/中段逐采样一致")

    # 要求6: 满幅处理不越界，采样数与采样率不变
    c6 = (all(-32768 <= s <= 32767 for s in sout)
          and p[2] == RATE and p[3] == n == len(sin))
    check(6, c6, "采样率%dHz, 采样数%d->%d, 无越界" % (p[2], n, p[3]))

    # 要求8: 损坏文件不影响其余片段
    c8 = (files["ep04_corrupt.wav"]["status"] == "failed"
          and files["ep04_corrupt.wav"]["skip_reason"]
          and files["ep01_ok.wav"]["status"] == "processed"
          and files["ep05_allsilence.wav"]["status"] == "processed"
          and os.path.isfile(os.path.join(OUT, "ep01_ok.wav"))
          and os.path.isfile(os.path.join(OUT, "ep05_allsilence.wav")))
    check(8, c8, "ep04 失败，ep01/ep05 正常产出")

    # 要求9: 合法JSON，计数吻合，未适用字段为null
    c9 = (report["total"] == 8
          and report["processed"] == 4
          and report["skipped"] == 3
          and report["failed"] == 1
          and report["total"] == report["processed"] + report["skipped"]
          + report["failed"]
          and files["ep02_stereo.wav"]["silence_intervals"] is None
          and files["ep02_stereo.wav"]["overwritten"] is None
          and files["ep01_ok.wav"]["skip_reason"] is None
          and files["ep01_ok.wav"]["overwritten"] is False)
    check(9, c9, "计数: %d/%d/%d/%d" % (report["total"], report["processed"],
                                          report["skipped"], report["failed"]))

    # 要求14: 有跳过片段时退出码为0，stderr 含被跳过片段名
    c14 = (r.returncode == 0 and "ep02_stereo.wav" in r.stderr
           and "ep04_corrupt.wav" in r.stderr)
    check(14, c14, "退出码=%d" % r.returncode)

    # ---------- 要求3: 阈值/时长参数可覆盖 ----------
    out3 = os.path.join(WORK, "out3")
    run_tool([SAMPLES, "-o", out3, "--min-silence", "1.5"])
    rep3 = json.load(open(os.path.join(out3, "report.json"),
                          encoding="utf-8"))
    iv3 = {e["name"]: e for e in rep3["files"]}["ep01_ok.wav"][
        "silence_intervals"]
    # ep08 间隙含 -30dB 哼声：默认 -40dB 阈值下 0 段静音，-20dB 下 1 段
    iv_def = files["ep08_hum.wav"]["silence_intervals"]
    out3b = os.path.join(WORK, "out3b")
    run_tool([SAMPLES, "-o", out3b, "--threshold-db", "-20"])
    rep3b = json.load(open(os.path.join(out3b, "report.json"),
                           encoding="utf-8"))
    iv3b = {e["name"]: e for e in rep3b["files"]}["ep08_hum.wav"][
        "silence_intervals"]
    c3 = (len(iv) == 2 and len(iv3) == 1
          and len(iv_def) == 0 and len(iv3b) == 1
          and abs(iv3b[0]["start_ms"] - 1000) <= 1
          and abs(iv3b[0]["duration_ms"] - 2000) <= 1)
    check(3, c3, "min=1.5s: 2->%d段; ep08阈值-40->-20dB: %d->%d段"
          % (len(iv3), len(iv_def), len(iv3b)))

    # ---------- 要求7: 同名覆盖并注明 ----------
    victim = os.path.join(OUT, "ep01_ok.wav")
    with open(victim, "wb") as fh:
        fh.write(b"OLD-CONTENT")
    r7 = run_tool([SAMPLES, "-o", OUT])
    rep7 = json.load(open(os.path.join(OUT, "report.json"),
                          encoding="utf-8"))
    e7 = {e["name"]: e for e in rep7["files"]}["ep01_ok.wav"]
    with open(victim, "rb") as fh:
        head = fh.read(4)
    c7 = e7["overwritten"] is True and head == b"RIFF"
    check(7, c7, "预置内容被替换，报告 overwritten=true")

    # ---------- 要求10,11,12: 清单顺序/过滤/缺失 ----------
    list_path = os.path.join(WORK, "list.txt")
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("# 本期节目清单\n"
                 "\n"
                 "ep05_allsilence.wav\n"
                 "ep01_ok.wav\n"
                 "ep99_ghost.wav\n")
    out_l = os.path.join(WORK, "out_list")
    r10 = run_tool([SAMPLES, "-o", out_l, "--list", list_path])
    rep_l = json.load(open(os.path.join(out_l, "report.json"),
                           encoding="utf-8"))
    names_l = [e["name"] for e in rep_l["files"]]
    c10 = names_l == ["ep05_allsilence.wav", "ep01_ok.wav", "ep99_ghost.wav"]
    check(10, c10, "报告顺序: %s" % names_l)

    c11 = (rep_l["total"] == 3 and rep_l["processed"] == 2
           and not os.path.exists(os.path.join(out_l, "ep02_stereo.wav"))
           and not os.path.exists(os.path.join(out_l, "ep06_fullscale.wav")))
    check(11, c11, "清单仅2个有效文件被处理，其余未触及")

    e_ghost = rep_l["files"][2]
    c12 = (e_ghost["status"] == "skipped"
           and "不存在" in e_ghost["skip_reason"]
           and rep_l["files"][0]["status"] == "processed"
           and rep_l["files"][1]["status"] == "processed"
           and r10.returncode == 0)
    check(12, c12, "ep99_ghost 跳过并注明不存在，其余成功")

    # ---------- 要求13: 致命错误非零退出 ----------
    r13a = run_tool([os.path.join(WORK, "no_such_dir"), "-o", out_l])
    r13b = run_tool([SAMPLES, "-o", out_l, "--list",
                     os.path.join(WORK, "no_such_list.txt")])
    c13 = (r13a.returncode != 0 and r13a.stderr.strip()
           and r13b.returncode != 0 and r13b.stderr.strip())
    check(13, c13, "退出码: 目录=%d, 清单=%d"
          % (r13a.returncode, r13b.returncode))

    # ---------- 要求15: --help / --version ----------
    rh = subprocess.run([sys.executable, TOOL, "--help"],
                        capture_output=True, text=True)
    rv = subprocess.run([sys.executable, TOOL, "--version"],
                        capture_output=True, text=True)
    import re
    c15 = (rh.returncode == 0 and "--threshold-db" in rh.stdout
           and "--list" in rh.stdout and "--min-silence" in rh.stdout
           and rv.returncode == 0
           and re.search(r"\d+\.\d+\.\d+", rv.stdout))
    check(15, c15, "版本输出: %s" % rv.stdout.strip())

    print("\n==== 结果: %d 项通过, %d 项失败 ====" % (len(passed), len(failed)))
    if failed:
        print("未通过要求:", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
