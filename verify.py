#!/usr/bin/env python3
"""验收脚本：逐条运行任务验收项并打印结果。"""
import json
import os
import shutil
import struct
import subprocess
import sys
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "audio_clip.py")
SAMPLES = os.path.join(ROOT, "samples")
OUT_BASE = os.path.join(ROOT, "out_verify")

MONO = os.path.join(SAMPLES, "mono_16bit.wav")
STEREO = os.path.join(SAMPLES, "stereo_16bit.wav")
CORRUPT = os.path.join(SAMPLES, "corrupt.txt")

results = []


def run(args):
    return subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True)


def fresh_dir(name):
    d = os.path.join(OUT_BASE, name)
    os.makedirs(d, exist_ok=True)
    return d


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    if os.path.isdir(OUT_BASE):
        shutil.rmtree(OUT_BASE)

    # 1. 无参数 -> 非零退出并打印用法；--help 列出全部参数
    r = run([])
    check("1a 无参数非零退出并打印用法",
          r.returncode != 0 and "usage" in (r.stderr + r.stdout).lower())
    r = run(["--help"])
    help_text = r.stdout
    check("1b --help 列出全部参数",
          r.returncode == 0 and all(k in help_text for k in
              ["--input", "--output-dir", "--start", "--end", "--fade-in", "--fade-out"]))

    # 2/7. 立体声输入 -> 可读原因失败 + 失败报告 + 无输出WAV
    d = fresh_dir("stereo")
    r = run(["--input", STEREO, "--output-dir", d,
             "--start", "1", "--end", "3", "--fade-in", "0.5", "--fade-out", "0.5"])
    msg = r.stderr + r.stdout
    check("2 立体声提示仅支持单声道且非零退出",
          r.returncode != 0 and "单声道" in msg, msg.strip().splitlines()[0] if msg.strip() else "")
    rep_path = os.path.join(d, "report.json")
    rep = json.load(open(rep_path, encoding="utf-8")) if os.path.isfile(rep_path) else None
    wavs = [f for f in os.listdir(d) if f.lower().endswith(".wav")]
    check("7 失败报告存在、状态失败、错误非空、目录无新WAV",
          rep is not None and rep["status"] == "failed" and rep["error"] and not wavs,
          f"error={rep['error'] if rep else None}")

    # 3. 区间倒置 -> 说明原因失败
    d = fresh_dir("bad_range")
    r = run(["--input", MONO, "--output-dir", d,
             "--start", "2", "--end", "1", "--fade-in", "0.1", "--fade-out", "0.1"])
    check("3 起始2秒结束1秒提示区间无效且非零退出",
          r.returncode != 0 and "区间无效" in (r.stderr + r.stdout))

    # 3b. 越界
    d = fresh_dir("oob")
    r = run(["--input", MONO, "--output-dir", d,
             "--start", "4", "--end", "9", "--fade-in", "0.1", "--fade-out", "0.1"])
    check("3b 结束秒超出时长提示原因且非零退出",
          r.returncode != 0 and "超出" in (r.stderr + r.stdout))

    # 4. 渐变总长超片段 -> 具体原因失败
    d = fresh_dir("bad_fade")
    r = run(["--input", MONO, "--output-dir", d,
             "--start", "1", "--end", "2", "--fade-in", "0.8", "--fade-out", "0.8"])
    check("4 片段1秒淡入0.8淡出0.8提示渐变总长超片段且非零退出",
          r.returncode != 0 and "渐变总长" in (r.stderr + r.stdout))

    # 5/6. 成功路径：帧数、首末帧为0、报告字段与时长精度
    d = fresh_dir("success")
    r = run(["--input", MONO, "--output-dir", d,
             "--start", "1.0", "--end", "3.5", "--fade-in", "0.5", "--fade-out", "0.5"])
    out_wav = os.path.join(d, "mono_16bit_clip.wav")
    ok5 = r.returncode == 0 and os.path.isfile(out_wav)
    detail5 = ""
    if ok5:
        with wave.open(out_wav, "rb") as w:
            rate, width, n = w.getframerate(), w.getsampwidth(), w.getnframes()
            frames = w.readframes(n)
        first = struct.unpack("<h", frames[:2])[0]
        last = struct.unpack("<h", frames[-2:])[0]
        expect = round((3.5 - 1.0) * 8000)
        ok5 = (rate == 8000 and width == 2 and n == expect
               and first == 0 and last == 0)
        detail5 = f"rate={rate} width={width} frames={n}/{expect} first={first} last={last}"
    check("5 输出采样率/位深一致、帧数正确、首末帧样本为0", ok5, detail5)

    rep = json.load(open(os.path.join(d, "report.json"), encoding="utf-8"))
    fields = {"status", "input_file", "output_file", "original_duration_sec",
              "clipped_duration_sec", "fade_in_sec", "fade_out_sec"}
    dur_ok = (abs(rep["original_duration_sec"] - 5.0) <= 0.001
              and abs(rep["clipped_duration_sec"] - 2.5) <= 0.001)
    check("6 报告字段齐全、状态成功、时长误差<=1ms",
          fields <= set(rep) and rep["status"] == "success" and dur_ok,
          json.dumps(rep, ensure_ascii=False))

    # 8. 输出目录不存在 -> 提示无法写入、非零退出、目录未被创建
    missing = os.path.join(OUT_BASE, "no_such_dir")
    r = run(["--input", MONO, "--output-dir", missing,
             "--start", "0", "--end", "1", "--fade-in", "0.1", "--fade-out", "0.1"])
    msg = r.stderr + r.stdout
    check("8 目录不存在提示无法写入、非零退出、目录未被创建",
          r.returncode != 0 and "无法写入" in msg and "报告" in msg
          and not os.path.exists(missing))

    # 9. 损坏输入 -> 友好提示、无Traceback、失败报告
    d = fresh_dir("corrupt")
    r = run(["--input", CORRUPT, "--output-dir", d,
             "--start", "0", "--end", "1", "--fade-in", "0.1", "--fade-out", "0.1"])
    msg = r.stderr + r.stdout
    rep_ok = os.path.isfile(os.path.join(d, "report.json"))
    check("9 纯文本输入提示无法读取WAV、无Traceback、非零退出、有失败报告",
          r.returncode != 0 and "无法读取WAV" in msg
          and "Traceback" not in msg and rep_ok)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
