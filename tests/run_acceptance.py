# -*- coding: utf-8 -*-
"""
run_acceptance.py — 对照任务 10 条验收项执行端到端验收。

方式：以子进程方式调用 src/wav_merger.py（与真实 CLI 使用方式一致），
检查退出码、stderr 文案、输出 WAV 头/样本、JSON 报告字段。
仅使用标准库。通过则退出码 0，任一失败退出码 1。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))

# Windows 控制台可能为 GBK 代码页，统一按 UTF-8 输出，避免打印中文/替换符崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
ROOT = os.path.join(HERE, "..")
SRC = os.path.join(ROOT, "src", "wav_merger.py")
SAMPLES = os.path.join(ROOT, "samples")
WAV_DIR = os.path.join(SAMPLES, "wav")
MARK_DIR = os.path.join(SAMPLES, "markers")
WORK = os.path.join(ROOT, "build", "acceptance")
SR = 44100

results = []


def record(name, ok, detail=""):
    results.append((name, ok, detail))
    print("[{}] {} {}".format("PASS" if ok else "FAIL", name, ("- " + detail) if detail else ""))


def run_cli(args, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 子进程 stderr/stdout 按 UTF-8 输出
    proc = subprocess.run(
        [sys.executable, "-I", SRC] + args,
        cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env
    )
    return proc.returncode, proc.stdout, proc.stderr


def read_wav(path):
    with wave.open(path, "rb") as wf:
        info = {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "framerate": wf.getframerate(),
            "nframes": wf.getnframes(),
        }
        raw = wf.readframes(info["nframes"])
    info["samples"] = struct.unpack("<{}h".format(info["nframes"] * info["channels"]), raw)
    return info


def read_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def expect_fail(name, args, expected_text, cwd=None, output_should_not_exist=None):
    code, out, err = run_cli(args, cwd=cwd)
    ok = code != 0 and expected_text in err
    detail = "exit={}, stderr={!r}".format(code, err.strip()[:120])
    if output_should_not_exist is not None:
        if os.path.exists(output_should_not_exist):
            ok = False
            detail += "；错误: 失败时仍生成了输出 WAV"
    record(name, ok, detail)


def main():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)

    stereo = os.path.join(WAV_DIR, "stereo_source.wav")
    mono = os.path.join(WAV_DIR, "mono_source.wav")
    levels = os.path.join(WAV_DIR, "stereo_levels.wav")
    bit8 = os.path.join(WAV_DIR, "source_8bit.wav")

    # ---------- 验收 1：8-bit WAV 必须被拒绝 ----------
    expect_fail(
        "验收1 8-bit WAV 报“不支持的样本格式”且非零退出",
        [bit8, os.path.join(MARK_DIR, "markers_two_halves.json"),
         os.path.join(WORK, "t1.wav")],
        "不支持的样本格式",
        output_should_not_exist=os.path.join(WORK, "t1.wav"),
    )

    # ---------- 验收 2：start == end 区间无效 ----------
    expect_fail(
        "验收2 start=end 报告区间无效且非零退出",
        [stereo, os.path.join(MARK_DIR, "markers_invalid_equal.json"),
         os.path.join(WORK, "t2.wav")],
        "区间无效",
        output_should_not_exist=os.path.join(WORK, "t2.wav"),
    )

    # ---------- 验收 3：44.1kHz 立体声两段各 0.5s ----------
    t3 = os.path.join(WORK, "t3.wav")
    code, out, err = run_cli([stereo, os.path.join(MARK_DIR, "markers_two_halves.json"), t3])
    if code == 0:
        info = read_wav(t3)
        expect_frames = 2 * (SR // 2)  # 两段各 0.5s
        ok = (info["nframes"] == expect_frames and info["channels"] == 2
              and info["framerate"] == SR and info["sampwidth"] == 2)
        record("验收3 立体声两段拼接: 帧数=两段之和且声道/采样率不变", ok,
               "frames={}, channels={}, rate={}".format(
                   info["nframes"], info["channels"], info["framerate"]))
    else:
        record("验收3 立体声两段拼接", False, "exit={}, stderr={!r}".format(code, err))

    # ---------- 验收 4：end 超出 0.5s，截断 + 警告，不中断 ----------
    t4 = os.path.join(WORK, "t4.wav")
    code, out, err = run_cli([stereo, os.path.join(MARK_DIR, "markers_overflow.json"), t4])
    if code == 0:
        info = read_wav(t4)
        rep = read_report(os.path.join(WORK, "t4_report.json"))
        # 区间 2.0s -> 3.5s 截断为 2.0s -> 3.0s，即 1.0s
        ok = (info["nframes"] == SR and any("截断" in w for w in rep["warnings"]))
        record("验收4 末尾超长截断并记录警告、WAV 仍有效", ok,
               "frames={}, warnings={}".format(info["nframes"], rep["warnings"]))
    else:
        record("验收4 末尾超长处理", False, "exit={}, stderr={!r}".format(code, err))

    # ---------- 验收 5：10ms 交叉淡化，总样本数不变且连接处渐变 ----------
    t5a = os.path.join(WORK, "t5_no_fade.wav")
    t5b = os.path.join(WORK, "t5_fade.wav")
    c1, _, e1 = run_cli([levels, os.path.join(MARK_DIR, "markers_crossfade.json"), t5a])
    c2, _, e2 = run_cli([levels, os.path.join(MARK_DIR, "markers_crossfade.json"),
                         t5b, "--crossfade", "10"])
    if c1 == 0 and c2 == 0:
        a = read_wav(t5a)
        b = read_wav(t5b)
        same_len = a["nframes"] == b["nframes"]
        fade_n = int(round(0.010 * SR))  # 441 帧
        junction = SR  # 两段各 1s，连接处位于第 SR 帧
        # 前一段尾部淡出：连接处前 fade_n 帧内，样本绝对值应整体小于无淡化电平
        tail_no = [abs(a["samples"][2 * (junction - 1 - k)]) for k in range(fade_n)]
        tail_fa = [abs(b["samples"][2 * (junction - 1 - k)]) for k in range(fade_n)]
        # 后一段头部淡入：连接处后 fade_n 帧内，渐变且首帧接近 0
        head_fa = [abs(b["samples"][2 * (junction + k)]) for k in range(fade_n)]
        tail_shrinks = sum(1 for x, y in zip(tail_no, tail_fa) if y < x) >= int(fade_n * 0.9)
        first_near_zero = head_fa[0] <= 1
        monotonic_head = all(head_fa[k] <= head_fa[k + 1] + 1 for k in range(0, fade_n - 1, 17))
        gradient = tail_shrinks and first_near_zero and monotonic_head
        record("验收5 10ms 交叉淡化: 帧数一致且连接处渐变",
               same_len and gradient,
               "frames={}/{}, 尾部衰减点数达标={}, 首帧={}, 头部单调={}".format(
                   a["nframes"], b["nframes"], tail_shrinks, head_fa[0], monotonic_head))
    else:
        record("验收5 交叉淡化", False, "exit={}/{}, err={!r} {!r}".format(c1, c2, e1, e2))

    # ---------- 验收 6：报告字段完整，无警告时 warnings 为空数组 ----------
    rep3 = read_report(os.path.join(WORK, "t3_report.json"))
    ok = (isinstance(rep3.get("output_path"), str)
          and isinstance(rep3.get("merged_segments"), int)
          and isinstance(rep3.get("warnings"), list)
          and isinstance(rep3.get("duration"), (int, float)))
    record("验收6 报告含 output_path/整数 merged_segments/数组 warnings", ok,
           "keys={}, merged_segments={}, warnings={}".format(
               sorted(rep3.keys()), rep3["merged_segments"], rep3["warnings"]))
    record("验收6b 无警告场景 warnings 为空数组", rep3["warnings"] == [],
           "warnings={}".format(rep3["warnings"]))

    # ---------- 验收 7：空标记 -> 0.5s 静音 + “无片段输入”警告 ----------
    t7 = os.path.join(WORK, "t7.wav")
    code, out, err = run_cli([stereo, os.path.join(MARK_DIR, "markers_empty.json"), t7])
    if code == 0:
        info = read_wav(t7)
        rep = read_report(os.path.join(WORK, "t7_report.json"))
        ok = (info["nframes"] == SR // 2 and info["channels"] == 2
              and all(s == 0 for s in info["samples"])
              and any("无片段输入" in w for w in rep["warnings"]))
        record("验收7 空标记生成 0.5s 立体声静音并警告“无片段输入”", ok,
               "frames={}, all_zero={}".format(info["nframes"], all(s == 0 for s in info["samples"])))
    else:
        record("验收7 空标记处理", False, "exit={}, stderr={!r}".format(code, err))

    # ---------- 验收 8：声道独立，左偏移保持、右始终静音 ----------
    info = read_wav(t3)
    left = info["samples"][0::2]
    right = info["samples"][1::2]
    ok = all(v == 1000 for v in left) and all(v == 0 for v in right)
    record("验收8 立体声各声道独立: 左保持 +1000 偏移、右全程静音", ok,
           "left 唯一值={}, right 唯一值={}".format(sorted(set(left)), sorted(set(right))))

    # ---------- 验收 9：标记文件不存在 / JSON 损坏，非零退出且不产出 WAV ----------
    t9a = os.path.join(WORK, "t9a.wav")
    expect_fail(
        "验收9a 不存在的标记路径: 非零退出且无 WAV 产出",
        [stereo, os.path.join(MARK_DIR, "no_such_markers.json"), t9a],
        "标记文件不存在",
        output_should_not_exist=t9a,
    )
    t9b = os.path.join(WORK, "t9b.wav")
    expect_fail(
        "验收9b 损坏 JSON: 非零退出且无 WAV 产出",
        [stereo, os.path.join(MARK_DIR, "markers_bad_json.json"), t9b],
        "JSON 格式错误",
        output_should_not_exist=t9b,
    )

    # ---------- 验收 10：含空格目录 + 相对路径 ----------
    spaced = os.path.join(WORK, "spaced dir")
    os.makedirs(spaced, exist_ok=True)
    rel_wav = os.path.relpath(stereo, spaced)
    rel_markers = os.path.relpath(os.path.join(MARK_DIR, "markers_two_halves.json"), spaced)
    t10 = os.path.join("output folder", "merged track.wav")  # 输出也含空格且为相对路径
    code, out, err = run_cli([rel_wav, rel_markers, t10], cwd=spaced)
    abs_out = os.path.join(spaced, t10)
    if code == 0 and os.path.exists(abs_out):
        info = read_wav(abs_out)
        rep = read_report(os.path.join(spaced, "output folder", "merged track_report.json"))
        ok = info["nframes"] == SR and rep["channels"] == 2
        record("验收10 含空格目录 + 相对路径合并成功且 WAV 可读", ok,
               "frames={}, report={}".format(info["nframes"], rep["output_path"]))
    else:
        record("验收10 空格/相对路径", False, "exit={}, stderr={!r}".format(code, err))

    # ---------- 附加：小数边界整样本对齐 + 单声道链路 ----------
    ta = os.path.join(WORK, "ta.wav")
    code, out, err = run_cli([mono, os.path.join(MARK_DIR, "markers_fractional.json"), ta])
    if code == 0:
        info = read_wav(ta)
        expected = int(round(1.667 * SR)) - int(round(0.333 * SR))
        ok = info["channels"] == 1 and info["framerate"] == SR and info["nframes"] == expected
        record("附加 单声道小数边界对齐到整样本", ok,
               "frames={}, expected={}".format(info["nframes"], expected))
    else:
        record("附加 单声道小数边界", False, "exit={}, stderr={!r}".format(code, err))

    # ---------- 附加：start 超出时长被拒绝 ----------
    expect_fail(
        "附加 start 超出音频时长: 非零退出",
        [stereo, os.path.join(MARK_DIR, "markers_start_out.json"),
         os.path.join(WORK, "tb.wav")],
        "超出音频时长",
        output_should_not_exist=os.path.join(WORK, "tb.wav"),
    )

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n==== 验收结果: {}/{} 通过 ====".format(passed, total))
    if passed != total:
        print("失败项:")
        for name, ok, detail in results:
            if not ok:
                print("  - {} ({})".format(name, detail))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
