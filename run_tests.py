#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py —— wav_editor.py 的 10 条验收项自动化验证（仅标准库）。

运行：
    python -I run_tests.py
前置：先运行 python -I make_samples.py 生成 sample_data/
所有测试产物输出到 test_output/，独立用 wave 标准库读取结果做交叉校验。
"""

import array
import json
import os
import struct
import subprocess
import sys
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
EDITOR = os.path.join(ROOT, "wav_editor.py")
MANIFEST_DIR = os.path.join(ROOT, "sample_data", "manifests")
WAV_DIR = os.path.join(ROOT, "sample_data", "wav")
TEST_ROOT = os.path.join(ROOT, "test_output")

PASS, FAIL = 0, 0
FAILURES = []


def check(condition, message):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("    [PASS] %s" % message)
    else:
        FAIL += 1
        FAILURES.append(message)
        print("    [FAIL] %s" % message)


def run_editor(manifest_name, out_name):
    out_dir = os.path.join(TEST_ROOT, out_name)
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-I", EDITOR,
         os.path.join(MANIFEST_DIR, manifest_name), out_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return proc, out_dir


def read_wav(path):
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        n = wf.getnframes()
        raw = wf.readframes(n)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return {"rate": rate, "channels": channels, "width": width,
            "n_frames": n, "samples": samples}


def list_files(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def section(title):
    print("\n=== %s ===" % title)


# ---------------------------------------------------------------- T1/T2
def test_01_basic_and_timeline():
    section("验收1/2：三文件输出、片段时长与首尾相接")
    proc, out_dir = run_editor("manifest_01_basic.json", "t01")
    check(proc.returncode == 0, "退出码为 0")
    files = list_files(out_dir)
    check(files == ["markers.json", "output.wav", "report.json"],
          "输出目录恰有三份清单命名文件: %s" % files)
    info = read_wav(os.path.join(out_dir, "output.wav"))
    # 200ms@44100 = 8820 帧；B 全长 300ms@22050=6615 帧重采样 => 13230 帧
    check(info["n_frames"] == 8820 + 13230,
          "总帧数 22050（两片段时长之和），实际 %d" % info["n_frames"])
    check(abs(info["n_frames"] * 1000.0 / info["rate"] - 500.0) < 1e-9,
          "输出总时长 500ms")
    with open(os.path.join(out_dir, "markers.json"), encoding="utf-8") as f:
        mk = json.load(f)
    check(len(mk["markers"]) == 1 and mk["markers"][0]["name"] ==
          "mark-middle", "标记 JSON 含 1 个命名标记")


# ---------------------------------------------------------------- T3
def test_03_resample():
    section("验收3：采样率统一到首片段 44100Hz")
    proc, out_dir = run_editor("manifest_02_resample.json", "t03")
    check(proc.returncode == 0, "退出码为 0")
    info = read_wav(os.path.join(out_dir, "output.wav"))  # 标准库可读
    check(info["rate"] == 44100, "输出采样率为 44100Hz，实际 %d" % info["rate"])
    # A 300ms=13230 帧；B 6615 帧@22050 -> 13230 帧@44100
    check(info["n_frames"] == 26460,
          "帧数符合预期 26460，实际 %d" % info["n_frames"])
    check(info["channels"] == 1 and info["width"] == 2,
          "单声道 16 位格式保持")


# ---------------------------------------------------------------- T4
def test_04_overlap_clamp():
    section("验收4：重叠混音相加并限幅、无回绕")
    proc, out_dir = run_editor("manifest_03_overlap.json", "t04")
    check(proc.returncode == 0, "退出码为 0")
    info = read_wav(os.path.join(out_dir, "output.wav"))
    samples = info["samples"]
    check(all(-32768 <= v <= 32767 for v in samples),
          "全部 %d 个采样位于 [-32768, 32767]" % len(samples))
    check(max(samples) == 32767 and min(samples) == -32768,
          "重叠区出现双向硬限幅峰值(32767/-32768)，证明相加后限幅而非回绕")
    # 从源 WAV 实读推导预期总帧数：C 350ms@44100 + D 重采样后放置在 250ms 处
    src_c = read_wav(os.path.join(WAV_DIR, "c_44100_stereo_350ms.wav"))
    src_d = read_wav(os.path.join(WAV_DIR, "d_22050_stereo_350ms.wav"))
    d_at_44100 = int(round(src_d["n_frames"] * 44100 / 22050))
    expected = int(round(250 * 44100 / 1000.0)) + d_at_44100
    check(src_c["n_frames"] == 15435 and d_at_44100 == 15436,
          "源帧数核对: C=%d 帧, D 重采样后=%d 帧"
          % (src_c["n_frames"], d_at_44100))
    check(info["n_frames"] == expected,
          "重叠时间线总帧数 %d 与源推导一致" % expected)


# ---------------------------------------------------------------- T5
def test_05_empty():
    section("验收5：空片段 -> 0 帧，标记全部越界")
    proc, out_dir = run_editor("manifest_04_empty.json", "t05")
    check(proc.returncode == 0, "退出码为 0（空清单是合法输入）")
    info = read_wav(os.path.join(out_dir, "output.wav"))
    check(info["n_frames"] == 0, "输出 WAV 为 0 帧")
    with open(os.path.join(out_dir, "markers.json"), encoding="utf-8") as f:
        mk = json.load(f)
    check(mk["markers"] == [], "标记 JSON 的有效标记为空")
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as f:
        report = json.load(f)
    check(report["rejected_marker_count"] == 1
          and "越界" in report["rejected_markers"][0]["reason"],
          "报告列出该标记越界: %s" % report["rejected_markers"][0]["reason"])


# ---------------------------------------------------------------- T6
def test_06_marker_bounds():
    section("验收6：标记越界逐条拒绝并保留有效标记")
    proc, out_dir = run_editor("manifest_05_marker_bounds.json", "t06")
    check(proc.returncode == 0, "退出码为 0")
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as f:
        report = json.load(f)
    names_ok = [m["name"] for m in report["valid_markers"]]
    names_bad = [m["name"] for m in report["rejected_markers"]]
    check(names_ok == ["valid"], "有效标记保留: %s" % names_ok)
    check(sorted(names_bad) == ["negative", "over-end"],
          "两条越界标记均被拒绝: %s" % names_bad)
    reasons = " | ".join(m["reason"] for m in report["rejected_markers"])
    check("负数" in reasons and "超出输出总时长" in reasons,
          "报告逐条说明原因: %s" % reasons)


# ---------------------------------------------------------------- T7
def test_07_bad_inputs():
    section("验收7：文件缺失/非16位PCM -> 整单拒绝、零输出、非零退出")
    for manifest, out_name, needle in (
        ("manifest_06_missing_file.json", "t07a", "not_exist_clip.wav"),
        ("manifest_07_bad_format.json", "t07b", "e_8bit_44100.wav"),
    ):
        proc, out_dir = run_editor(manifest, out_name)
        check(proc.returncode != 0, "%s 非零退出码 %d"
              % (manifest, proc.returncode))
        check(list_files(out_dir) == [], "输出目录无新增文件")
        check(needle in proc.stderr, "终端错误包含问题文件路径 %s" % needle)


# ---------------------------------------------------------------- T8
def test_08_gain():
    section("验收8：增益范围校验与限幅")
    proc, out_dir = run_editor("manifest_08_gain_invalid.json", "t08a")
    check(proc.returncode != 0, "增益 +20dB 被拒绝（非零退出）")
    check(list_files(out_dir) == [], "拒绝时无输出文件")
    check("gain_db" in proc.stderr and "20" in proc.stderr,
          "错误指明 gain_db 字段与数值")

    proc, out_dir = run_editor("manifest_09_gain_ok.json", "t08b")
    check(proc.returncode == 0, "增益 +6dB 正常处理")
    info = read_wav(os.path.join(out_dir, "output.wav"))
    check(max(abs(v) for v in info["samples"]) <= 32767,
          "调整后峰值不超过 32767，实际峰值 %d"
          % max(abs(v) for v in info["samples"]))
    check(info["n_frames"] == 13230, "输出可被标准库读取且帧数正确")


# ---------------------------------------------------------------- T9
def test_09_overwrite_report():
    section("验收9：覆盖旧文件，报告数值与输出 WAV 一致")
    out_dir = os.path.join(TEST_ROOT, "t09")
    os.makedirs(out_dir, exist_ok=True)
    # 预置同名旧文件，内容与预期完全不同
    with wave.open(os.path.join(out_dir, "output.wav"), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(8000)
        wf.writeframes(array.array("h", [1, 2, 3, 4]).tobytes())
    with open(os.path.join(out_dir, "markers.json"), "w",
              encoding="utf-8") as f:
        f.write('{"stale": true}')
    with open(os.path.join(out_dir, "report.json"), "w",
              encoding="utf-8") as f:
        f.write('{"stale": true}')

    proc, _ = run_editor("manifest_10_overwrite.json", "t09")
    check(proc.returncode == 0, "退出码为 0")
    info = read_wav(os.path.join(out_dir, "output.wav"))
    with open(os.path.join(out_dir, "report.json"), encoding="utf-8") as f:
        report = json.load(f)
    with open(os.path.join(out_dir, "markers.json"), encoding="utf-8") as f:
        markers = json.load(f)
    check("stale" not in report and "stale" not in markers,
          "旧 JSON 文件已被覆盖")
    check(info["rate"] != 8000
          and info["rate"] == report["sample_rate"] == 44100,
          "旧文件已覆盖，且采样率报告一致(44100Hz)")
    check(info["channels"] == report["channels"],
          "声道数报告一致(%d)" % info["channels"])
    check(info["n_frames"] == report["total_frames"],
          "帧数报告一致(%d)" % info["n_frames"])
    check(abs(info["n_frames"] * 1000.0 / info["rate"]
              - report["total_duration_ms"]) < 1e-6,
          "总时长毫秒与 WAV 实算一致(%g)" % report["total_duration_ms"])
    check(report["valid_marker_count"] == len(markers["markers"]) == 1,
          "有效标记数报告一致(1)")


# ---------------------------------------------------------------- T10
def test_10_field_errors():
    section("验收10：非法字段整单拒绝并列出全部字段名")
    proc, out_dir = run_editor("manifest_11_field_errors.json", "t10")
    check(proc.returncode != 0, "非零退出码")
    check(list_files(out_dir) == [], "无任何输出文件")
    check("clips[0].start_ms" in proc.stderr,
          "报告列出 clips[0].start_ms 字段")
    check("clips[0].gain_db" in proc.stderr,
          "报告列出 clips[0].gain_db 字段")


def main():
    if not os.path.isdir(MANIFEST_DIR):
        print("缺少样本目录，请先运行: python -I make_samples.py")
        return 2
    os.makedirs(TEST_ROOT, exist_ok=True)
    for fn in (test_01_basic_and_timeline, test_03_resample,
               test_04_overlap_clamp, test_05_empty, test_06_marker_bounds,
               test_07_bad_inputs, test_08_gain, test_09_overwrite_report,
               test_10_field_errors):
        fn()

    print("\n========== 汇总 ==========")
    print("PASS: %d   FAIL: %d" % (PASS, FAIL))
    if FAIL:
        print("失败项:")
        for m in FAILURES:
            print("  - " + m)
        return 1
    print("全部验收项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
