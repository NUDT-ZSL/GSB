#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_acceptance.py — 对 audio_trimmer.py 的 9 项验收要求逐项实测。

每个场景在 verify_workspace/ 下独立建目录，通过子进程真实运行工具，
全部通过则退出码 0，否则非 0。
"""
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "audio_trimmer.py"
WORK = ROOT / "verify_workspace"

results = []  # (编号, 是否通过, 说明)


def check(req, ok, detail=""):
    results.append((req, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] 要求{req}: {detail}")


def run_tool(*args):
    """运行工具，返回 (退出码, stdout, stderr)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")  # 子进程统一以 UTF-8 输出中文
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def write_wav(path, samples, rate=44100, channels=1):
    frames = []
    if channels == 1:
        frames = list(samples)
    else:
        for frame in samples:
            frames.extend(frame)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(frames), *frames))


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        params = (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())
        raw = w.readframes(params[3])
    samples = list(struct.unpack("<%dh" % (len(raw) // 2), raw))
    return params, samples


def sine(freq, seconds, rate, amp=int(0.6 * 32767)):
    n = int(round(seconds * rate))
    return [int(amp * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]


def silence(seconds, rate):
    return [0] * int(round(seconds * rate))


def fresh_dir(name):
    d = WORK / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def load_manifest(out_dir):
    return json.loads((Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 要求 1
def req1():
    inp = fresh_dir("req1_input")
    out = fresh_dir("req1_output")
    write_wav(inp / "b_第二个.wav", sine(440, 0.5, 44100))
    write_wav(inp / "a_第一个.wav", sine(440, 0.5, 44100))
    (inp / "说明.txt").write_text("应被忽略", encoding="utf-8")
    code, _, _ = run_tool(inp, out)
    entries = load_manifest(out)
    names = [Path(e["source"]).name for e in entries]
    ok = (
        code == 0
        and names == ["a_第一个.wav", "b_第二个.wav"]
        and all(e["status"] == "成功" for e in entries)
    )
    check(1, ok, f"清单条目={names}（txt 已忽略，按文件名升序）")


# ---------------------------------------------------------------- 要求 2
def req2():
    inp = fresh_dir("req2_input")
    out = fresh_dir("req2_output")
    rate = 44100
    src = sine(440, 0.75, rate) + silence(0.5, rate) + sine(440, 0.75, rate)
    write_wav(inp / "sine_gap.wav", src, rate)
    code, _, _ = run_tool(inp, out)
    entries = load_manifest(out)
    e = entries[0]
    if code != 0 or e["status"] != "成功":
        check(2, False, f"处理失败: {e}")
        return
    (ch, sw, fr, nframes), out_samples = read_wav(e["output"])
    dur = nframes / fr
    # 源非静音段首尾样本（阈值 -45dBFS -> 幅值约 184）
    thr = 32767 * 10 ** (-45 / 20)
    loud = [i for i, s in enumerate(src) if abs(s) >= thr]
    head_ok = out_samples[0] == src[loud[0]]
    tail_ok = out_samples[-1] == src[loud[-1]]
    ok = abs(dur - 1.5) < 0.01 and head_ok and tail_ok
    check(
        2,
        ok,
        f"输出时长={dur:.4f}s（期望≈1.5s），首样本一致={head_ok}，尾样本一致={tail_ok}",
    )


# ---------------------------------------------------------------- 要求 3
def req3():
    inp = fresh_dir("req3_input")
    out = fresh_dir("req3_output")
    rate = 44100
    left = sine(440, 1.0, rate)
    right = sine(550, 1.0, rate)
    write_wav(inp / "立体声.wav", list(zip(left, right)), rate, channels=2)
    code, _, _ = run_tool(inp, out)
    e = load_manifest(out)[0]
    (ch, sw, fr, _), _ = read_wav(e["output"])
    ok = code == 0 and ch == 1 and sw == 2 and fr == 44100
    check(3, ok, f"输出声道数={ch}、位深={sw * 8}、采样率={fr}")


# ---------------------------------------------------------------- 要求 4
def req4():
    inp = fresh_dir("req4_input")
    out = fresh_dir("req4_output")
    write_wav(inp / "全静音.wav", silence(1.0, 22050), 22050)
    code, _, _ = run_tool(inp, out)
    e = load_manifest(out)[0]
    no_file = not any(p.suffix == ".wav" for p in out.iterdir())
    ok = (
        code == 0
        and e["status"] == "无有效音频"
        and e["output"] is None
        and e["error"] is None
        and no_file
    )
    check(4, ok, f"状态={e['status']}，输出路径={e['output']}，输出目录无 wav={no_file}")


# ---------------------------------------------------------------- 要求 5
def req5():
    inp = fresh_dir("req5_input")
    out = fresh_dir("req5_output")
    write_wav(inp / "正常.wav", sine(440, 0.5, 44100))
    (inp / "损坏.wav").write_bytes(b"RIFF\x04\x00\x00\x00WAVEbroken")
    code, _, _ = run_tool(inp, out)
    entries = {Path(e["source"]).name: e for e in load_manifest(out)}
    good = entries["正常.wav"]
    bad = entries["损坏.wav"]
    ok = (
        code == 0
        and good["status"] == "成功"
        and Path(good["output"]).exists()
        and bad["status"] == "失败"
        and bad["error"]
    )
    check(5, ok, f"正常文件状态={good['status']}，损坏文件错误={bad['error']!r}")


# ---------------------------------------------------------------- 要求 6
def req6():
    inp = fresh_dir("req6_input")
    out = fresh_dir("req6_output")
    write_wav(inp / "ok.wav", sine(440, 0.5, 44100))
    (inp / "bad.wav").write_bytes(b"not a wav")
    write_wav(inp / "mute.wav", silence(0.5, 44100))
    run_tool(inp, out)
    text = (out / "manifest.json").read_text(encoding="utf-8")
    try:
        entries = json.loads(text)  # 合法 JSON
    except json.JSONDecodeError as exc:
        check(6, False, f"清单不是合法 JSON: {exc}")
        return
    fields_ok = all(
        set(e) >= {"source", "output", "status", "error"} for e in entries
    )
    by_status = {e["status"]: e for e in entries}
    ok = (
        fields_ok
        and Path(by_status["成功"]["output"]).exists()
        and by_status["成功"]["error"] is None
        and by_status["失败"]["error"]
        and by_status["无有效音频"]["output"] is None
    )
    check(6, ok, "清单为合法 JSON，字段齐全，成功/失败/无有效音频条目内容正确")


# ---------------------------------------------------------------- 要求 7
def req7():
    inp = fresh_dir("req7_input")
    out = fresh_dir("req7_output")
    write_wav(inp / "dup.wav", sine(440, 0.5, 44100))
    sentinel = out / "dup_processed.wav"
    sentinel.write_bytes(b"PRE-EXISTING-CONTENT")  # 预置同名输出
    code, _, _ = run_tool(inp, out)
    e = load_manifest(out)[0]
    kept = sentinel.read_bytes() == b"PRE-EXISTING-CONTENT"
    new_path = out / "dup_processed_1.wav"
    ok = (
        code == 0
        and kept
        and new_path.exists()
        and e["output"] == str(new_path)
        and e["status"] == "成功"
    )
    check(7, ok, f"原文件未覆盖={kept}，新文件={new_path.name}，清单记录={Path(e['output']).name}")


# ---------------------------------------------------------------- 要求 8
def req8():
    inp = fresh_dir("req8_input")
    write_wav(inp / "a.wav", sine(440, 0.5, 44100))
    out1 = fresh_dir("req8_out_ok")
    code_ok, _, _ = run_tool(inp, out1, "--threshold", "-60", "--min-silence", "0.1")
    out2 = fresh_dir("req8_out_bad1")
    code_90, _, err90 = run_tool(inp, out2, "--threshold", "-90")
    out3 = fresh_dir("req8_out_bad2")
    code_1, _, err1 = run_tool(inp, out3, "--threshold", "-1")
    ok = (
        code_ok == 0
        and code_90 != 0
        and "阈值" in err90
        and code_1 != 0
        and "阈值" in err1
    )
    check(
        8,
        ok,
        f"-60/0.1 退出码={code_ok}；-90 退出码={code_90}；-1 退出码={code_1}（均带参数提示）",
    )


# ---------------------------------------------------------------- 要求 9
def req9():
    inp = fresh_dir("req9_input")
    out = fresh_dir("req9_output")
    write_wav(inp / "good.wav", sine(440, 0.5, 44100))
    (inp / "broken.wav").write_bytes(b"garbage")
    write_wav(inp / "silent.wav", silence(0.5, 44100))
    code, stdout, _ = run_tool(inp, out)
    ok = (
        code == 0
        and "处理总数: 3" in stdout
        and "成功: 1" in stdout
        and "失败: 1" in stdout
        and "无有效音频: 1" in stdout
        and "broken.wav" in stdout
    )
    check(9, ok, "摘要显示 总数3/成功1/失败1/无有效音频1，并列出失败文件 broken.wav")
    print("----- 摘要实际输出 -----")
    print(stdout.strip())
    print("------------------------")


def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    for fn in (req1, req2, req3, req4, req5, req6, req7, req8, req9):
        fn()
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== 验收结果: {passed}/{len(results)} 项通过 =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
