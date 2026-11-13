#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_demo.py —— 端到端验收：自动覆盖任务书 15 条验收标准

运行方式（在项目根目录）:
    python -I make_samples.py     # 先生成确定性素材（已生成可跳过）
    python -I run_demo.py

每个用例在独立目录 demo_runs/Txx_xxx/ 中以子进程方式调用 podcast_mixer.py，
全部通过时退出码 0；任一失败退出码 1。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

# 隔离模式(-I)下解释器不自动注入脚本目录，显式加入以导入同目录主工具
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import podcast_mixer as pm

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")
RUNS = os.path.join(ROOT, "demo_runs")
MIXER = os.path.join(ROOT, "podcast_mixer.py")

A_WAV = os.path.join(SAMPLES, "trackA_44100_mono.wav")
B_WAV = os.path.join(SAMPLES, "trackB_22050_mono.wav")
STEREO_WAV = os.path.join(SAMPLES, "stereo_44100.wav")
SILENCE_WAV = os.path.join(SAMPLES, "silence_44100_mono.wav")
NOTAUDIO = os.path.join(SAMPLES, "notaudio.wav")
LOWPEAK = os.path.join(SAMPLES, "lowpeak_44100_mono.wav")
CLIP_A = os.path.join(SAMPLES, "clipA_44100.wav")
CLIP_B = os.path.join(SAMPLES, "clipB_44100.wav")
M_DEMO = os.path.join(SAMPLES, "markers_demo.txt")
M_BAD = os.path.join(SAMPLES, "markers_bad.txt")
M_SHUF = os.path.join(SAMPLES, "markers_shuffled.txt")
M_NOLBL = os.path.join(SAMPLES, "markers_nolabel.txt")

results: list[tuple[str, bool, str]] = []


def case(name: str):
    def deco(fn):
        def wrapper():
            case_dir = os.path.join(RUNS, name)
            if os.path.exists(case_dir):
                shutil.rmtree(case_dir)
            os.makedirs(case_dir)
            try:
                detail = fn(case_dir)
                results.append((name, True, detail or "OK"))
            except AssertionError as exc:
                results.append((name, False, f"断言失败: {exc}"))
            except Exception as exc:  # noqa: BLE001
                results.append((name, False, f"{type(exc).__name__}: {exc}"))
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def run_cli(case_dir: str, *extra: str):
    out = os.path.join(case_dir, "out.wav")
    cmd = [sys.executable, "-I", MIXER, "--output", out, *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          cwd=ROOT)
    return proc, out, out.replace(".wav", ".json")


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ---------------- T1: 非 WAV / 损坏头必须明确拒绝且不产出 ----------------
@case("T01_reject_non_wav")
def t01(case_dir):
    proc, out_wav, out_json = run_cli(case_dir, "--track-a", NOTAUDIO, "--track-b", B_WAV)
    check(proc.returncode != 0, "假 WAV 输入应返回非零退出码")
    check("无法识别" in proc.stderr, f"stderr 应提示无法识别音频，实际: {proc.stderr!r}")
    check(not os.path.exists(out_wav), "失败时不得产出 WAV 半成品")
    check(not os.path.exists(out_json), "失败时不得产出 JSON 半成品")
    return f"退出码={proc.returncode}，输出目录无成品文件"


# ---------------- T2: 采样率统一到较高者 ----------------
@case("T02_resample_to_higher_rate")
def t02(case_dir):
    proc, out_wav, out_json = run_cli(case_dir, "--track-a", A_WAV, "--track-b", B_WAV)
    check(proc.returncode == 0, proc.stderr)
    info = pm.read_wav(out_wav)
    report = load_json(out_json)
    check(info["sample_rate"] == 44100, f"成品应为 44100Hz，实际 {info['sample_rate']}")
    check(report["sample_rate"] == 44100, "报告采样率字段错误")
    check(report["tracks"]["A"]["original_sample_rate"] == 44100, "A 原始采样率记录错误")
    check(report["tracks"]["A"]["target_sample_rate"] == 44100, "A 目标采样率记录错误")
    check(report["tracks"]["B"]["original_sample_rate"] == 22050, "B 原始采样率记录错误")
    check(report["tracks"]["B"]["target_sample_rate"] == 44100, "B 目标采样率记录错误")
    return "22050+44100 → 成品 44100Hz，报告原始/目标采样率齐全"


# ---------------- T3: 立体声仅取第一声道并标注降混 ----------------
@case("T03_downmix_stereo_first_channel")
def t03(case_dir):
    proc, out_wav, out_json = run_cli(case_dir, "--track-a", STEREO_WAV, "--track-b", B_WAV)
    check(proc.returncode == 0, proc.stderr)
    info = pm.read_wav(out_wav)
    report = load_json(out_json)
    check(info["channels"] == 1, f"成品应为单声道，实际 {info['channels']} 声道")
    check(report["channels"] == 1, "报告声道字段错误")
    check(report["tracks"]["A"]["downmixed_to_mono"] is True, "报告应标注 A 已降为单声道")
    peak_before = report["normalization"][0]["peak_before"]
    check(peak_before == 20000, f"应取左声道(峰值20000)，实际归一化前峰值 {peak_before}")
    return f"立体声输入 → 单声道，取第一声道(峰值{peak_before})，报告标注降混"


# ---------------- T4: 越界偏移截断并注明 ----------------
@case("T04_clamp_out_of_range_offsets")
def t04(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--start-a", "30000", "--end-a", "999999")
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    info = pm.read_wav(out_wav)
    warns = " | ".join(report["tracks"]["A"]["offset_warnings"])
    check("截断" in warns, f"报告应注明偏移截断，实际警告: {warns}")
    # A 原始 246960 帧，[30000, 246960) 内仅剩第二段语音 [233730, 246960)
    check(report["tracks"]["A"]["used_end"] == 246960,
          f"结束偏移应截断到 246960，实际 {report['tracks']['A']['used_end']}")
    check(info["frame_count"] == 13230, f"成品帧数应为剩余有效采样 13230，实际 {info['frame_count']}")

    # 起始偏移整体越界：该轨无效区间为空，单独运行应失败退出
    bad_dir = os.path.join(case_dir, "overflow")
    os.makedirs(bad_dir)
    proc2, out2, _ = run_cli(bad_dir, "--track-a", A_WAV, "--start-a", "999999")
    check(proc2.returncode != 0, "起始偏移整体越界且无其他轨时应失败")
    check(not os.path.exists(out2), "失败不得产出文件")
    return f"结束偏移截断至 246960，成品 {info['frame_count']} 帧；整体越界正确报错"


# ---------------- T5: 5 秒静音压缩为 0.3 秒 ----------------
@case("T05_silence_compression")
def t05(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--track-b", B_WAV, "--markers", M_DEMO)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    cuts = [c for c in report["silence_cuts"]
            if c["track"] == "A" and c["length"] == 220500]
    check(len(cuts) == 1, f"应记录一次 220500 采样(5s)静音跳过，实际 {report['silence_cuts']}")
    cut = cuts[0]
    check(cut["input_start"] == 13230 and cut["input_end"] == 233730,
          f"静音区间起止采样记录错误: {cut}")
    check(cut["kept_samples"] == 13230, f"应保留 13230(0.3s)，实际 {cut['kept_samples']}")
    check(report["total_frames"] == 88200, f"成品应 88200 帧(2.0s)，实际 {report['total_frames']}")
    pause_skips = [s for s in report["skipped"] if s["reason"] == "压缩停顿"]
    check(len(pause_skips) >= 1, "skipped 中应有压缩停顿记录")
    return ("5s 静音 [13230,233730) 压为 0.3s(13230 采样)，"
            f"成品 {report['total_frames']} 帧 / {report['duration_seconds']}s")


# ---------------- T6: 峰值归一化 + 全静音防除零 ----------------
@case("T06_peak_normalization")
def t06(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", LOWPEAK, "--track-b", SILENCE_WAV)
    check(proc.returncode == 0, f"全静音轨不得导致崩溃，stderr={proc.stderr}")
    report = load_json(out_json)
    norm = report["normalization"]
    check(len(norm) == 1 and norm[0]["peak_before"] == 800,
          f"低峰值段归一化记录错误: {norm}")
    check(norm[0]["peak_after"] == 28000, f"归一后峰值应为目标 28000，实际 {norm[0]['peak_after']}")
    info = pm.read_wav(out_wav)
    actual_peak = max(abs(v) for v in info["samples"])
    check(actual_peak == 28000, f"成品实际峰值 {actual_peak} != 目标 28000")
    return f"峰值 800 → 28000（增益 {norm[0]['gain']}），全静音轨安全跳过无除零"


# ---------------- T7: 重叠相加饱和限幅，无回绕爆音 ----------------
@case("T07_saturating_clipping")
def t07(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", CLIP_A, "--track-b", CLIP_B, "--place-b", "0")
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    check(report["clipping"]["event_count"] > 0, "应记录限幅事件")

    # 独立重建参考信号：两轨各自归一化后饱和相加
    a = pm.read_wav(CLIP_A)["samples"]
    b = pm.read_wav(CLIP_B)["samples"]
    a_n, *_ = pm.normalize_samples(a, 28000)
    b_n, *_ = pm.normalize_samples(b, 28000)
    raw_sum = [x + y for x, y in zip(a_n, b_n)]
    check(any(v > 32767 or v < -32768 for v in raw_sum), "测试构造必须确实超出 16bit 范围")
    ref = [pm._saturate(v) for v in raw_sum]

    got = list(pm.read_wav(out_wav)["samples"])
    check(got == ref, "成品样本必须等于饱和限幅参考值")
    for v, s in zip(got, raw_sum):
        if s != 0:
            check((v > 0) == (s > 0), f"出现符号翻转(回绕爆音): sum={s} out={v}")
        check(-32768 <= v <= 32767, "输出超出 16bit 范围")
    return f"限幅 {report['clipping']['event_count']} 处，全部饱和处理、无符号翻转"


# ---------------- T8: 标记驱动 source/label 标注 ----------------
@case("T08_marker_labels")
def t08(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--track-b", B_WAV, "--markers", M_DEMO)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    first = next(s for s in report["segments"] if s["start"] == 0)
    check(first["source"] == "A" and first["label"] == "开场白",
          f"首段 source/label 错误: {first}")
    b_first = next(s for s in report["segments"] if s["start"] == 39690)
    check(b_first["source"] == "B" and b_first["label"] == "B主播发言",
          f"B 首段 source/label 错误: {b_first}")
    b_last = next(s for s in report["segments"] if s["start"] == 70560)
    check(b_last["label"] == "B主播总结", f"B 末段标签错误: {b_last}")
    return "标记覆盖区间的 source 与 label 与标记文件完全一致"


# ---------------- T9: 非法标记行跳过且合法行生效 ----------------
@case("T09_skip_invalid_marker_rows")
def t09(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--track-b", B_WAV, "--markers", M_BAD)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    reasons = " || ".join(s["reason"] or "" for s in report["skipped"])
    check("负" in reasons, f"skipped 应包含负采样行说明，实际: {reasons}")
    check(len([s for s in report["skipped"] if "标记文件" in (s["reason"] or "")]) == 5,
          "5 行非法标记均应进入 skipped")
    first = next(s for s in report["segments"] if s["start"] == 0)
    second = next(s for s in report["segments"] if s["start"] == 26460)
    check(first["label"] == "合法开场", f"合法标记仍须生效，实际 {first['label']}")
    check(second["label"] == "合法第二句", f"第二句合法标记丢失，实际 {second['label']}")
    return "5 条非法行全部带原因跳过，2 条合法标记正常生效"


# ---------------- T10: 全静音轨无效，另一轨独立成品 ----------------
@case("T10_silent_track_invalid_standalone")
def t10(case_dir):
    d_single = os.path.join(case_dir, "single")
    d_both = os.path.join(case_dir, "both")
    os.makedirs(d_single)
    os.makedirs(d_both)
    p1, w1, j1 = run_cli(d_single, "--track-b", B_WAV)
    p2, w2, j2 = run_cli(d_both, "--track-a", SILENCE_WAV, "--track-b", B_WAV)
    check(p1.returncode == 0 and p2.returncode == 0, f"{p1.stderr} {p2.stderr}")
    with open(w1, "rb") as f:
        bytes_single = f.read()
    with open(w2, "rb") as f:
        bytes_both = f.read()
    check(bytes_single == bytes_both, "含全静音 A 轨的成品应与仅 B 轨成品字节一致")
    report = load_json(j2)
    check(report["tracks"]["A"]["valid"] is False, "报告应标注 A 轨无效")
    check("全静音" in report["tracks"]["A"]["invalid_reason"],
          f"无效原因应说明全静音: {report['tracks']['A']['invalid_reason']}")
    check(report["tracks"]["B"]["valid"] is True, "B 轨应保持有效并独立完成成品")
    return f"A 轨无效（{report['tracks']['A']['invalid_reason']}），成品与 B 独立运行逐字节相同"


# ---------------- T11: 输出读回校验，头部/数据/位宽自洽 ----------------
@case("T11_output_roundtrip_verify")
def t11(case_dir):
    proc, out_wav, out_json = run_cli(case_dir, "--track-a", A_WAV, "--track-b", B_WAV)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    info = pm.read_wav(out_wav)
    check(info["frame_count"] == report["total_frames"], "读回帧数与报告不一致")
    check(info["data_bytes_len"] == info["frame_count"] * 2, "data 字节数 != 帧数×2")
    with open(out_wav, "rb") as f:
        raw = f.read()
    bits = int.from_bytes(raw[34:36], "little")
    channels = int.from_bytes(raw[22:24], "little")
    check(bits == 16, f"位宽应为 16，实际 {bits}")
    check(channels == 1, f"应为单声道，实际 {channels}")
    riff_size = int.from_bytes(raw[4:8], "little")
    check(riff_size + 8 == len(raw), "RIFF 尺寸字段与文件实际长度不一致")
    return f"读回 {info['frame_count']} 帧 / {info['data_bytes_len']} 字节，16bit 单声道，头部自洽"


# ---------------- T12: 默认拒绝覆盖，--overwrite 才写入 ----------------
@case("T12_overwrite_protection")
def t12(case_dir):
    p1, out_wav, out_json = run_cli(case_dir, "--track-a", LOWPEAK)
    check(p1.returncode == 0, p1.stderr)
    before = open(out_wav, "rb").read()

    p2, _, _ = run_cli(case_dir, "--track-a", A_WAV, "--track-b", B_WAV)
    check(p2.returncode != 0, "同名文件存在且未加 --overwrite 时必须失败")
    check("覆盖" in p2.stderr, f"应提示覆盖保护，实际: {p2.stderr!r}")
    after = open(out_wav, "rb").read()
    check(before == after, "拒绝覆盖时原文件内容不得被修改")

    p3, _, _ = run_cli(case_dir, "--track-a", A_WAV, "--track-b", B_WAV, "--overwrite")
    check(p3.returncode == 0, p3.stderr)
    check(open(out_wav, "rb").read() != before, "加 --overwrite 后应写入新内容")
    return "无开关：失败且原文件不变；加 --overwrite：成功写入"


# ---------------- T13: 报告字段完整，无标签为 null，动作类型合法 ----------------
@case("T13_report_schema_null_label")
def t13(case_dir):
    proc, out_wav, out_json = run_cli(case_dir, "--track-a", A_WAV, "--track-b", B_WAV)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    allowed = {"合并", "跳过"}
    for seg in report["segments"]:
        for key in ("start", "end", "source", "label", "action", "reason"):
            check(key in seg, f"segment 缺少字段 {key}")
        check(seg["action"] in allowed, f"动作类型非法: {seg['action']}")
        check(seg["label"] is None, f"无标记时 label 必须为 null，实际 {seg['label']!r}")
    for sk in report["skipped"]:
        check(sk["action"] == "跳过", "skipped 记录动作类型必须为跳过")
        check("reason" in sk and sk["reason"], "跳过记录必须含原因")
    # 三字段标记文件同样应得到 null 标签
    p2, _, j2 = run_cli(os.path.join(case_dir, "nl"),
                        "--track-a", A_WAV, "--track-b", B_WAV, "--markers", M_NOLBL)
    check(p2.returncode == 0, p2.stderr)
    rep2 = load_json(j2)
    check(all(s["label"] is None for s in rep2["segments"]), "三字段标记的 label 应为 null")
    return "所有 segment 字段齐全，动作仅合并/跳过，无标签均为 null"


# ---------------- T14: segments 严格升序，跳过记录不影响排序 ----------------
@case("T14_segments_sorted")
def t14(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--track-b", B_WAV, "--markers", M_SHUF)
    check(proc.returncode == 0, proc.stderr)
    report = load_json(out_json)
    starts = [s["start"] for s in report["segments"]]
    check(starts == sorted(starts), f"segments 未升序: {starts}")
    check(len(starts) == len(set(starts)), "segments 起始采样存在重复")
    # 乱序输入的标签依然正确落到对应段
    last = report["segments"][-1]
    check(last["label"] == "最后一句", f"乱序标记未正确匹配: {last}")
    check(all("start" in s for s in report["skipped"]), "skipped 独立成列且结构完整")
    return f"{len(starts)} 个 segment 严格升序，乱序标记仍正确匹配"


# ---------------- T15: 端到端退出码与目录洁净度 ----------------
@case("T15_end_to_end_clean_dir")
def t15(case_dir):
    proc, out_wav, out_json = run_cli(
        case_dir, "--track-a", A_WAV, "--track-b", B_WAV,
        "--markers", M_DEMO, "--overwrite")
    check(proc.returncode == 0, f"成功路径退出码应为 0，实际 {proc.returncode}: {proc.stderr}")
    entries = sorted(os.listdir(case_dir))
    check(entries == ["out.json", "out.wav"], f"目录应仅含 1 WAV + 1 JSON，实际 {entries}")
    leftovers = [n for n in entries if n.startswith(".tmp") or n.endswith((".tmp", "~"))]
    check(not leftovers, f"存在临时残留: {leftovers}")
    report = load_json(out_json)
    check(report["total_frames"] == pm.read_wav(out_wav)["frame_count"], "成品帧数与报告不符")
    return f"退出码 0，目录仅 {entries}，无临时文件残留"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not os.path.isdir(SAMPLES):
        print("未找到 samples/，先执行: python -I make_samples.py")
        return 1
    os.makedirs(RUNS, exist_ok=True)

    tests = [obj for name, obj in sorted(globals().items())
             if name.startswith("t") and callable(obj) and getattr(obj, "__name__", "").startswith("t")]
    for fn in tests:
        fn()

    print("=" * 78)
    passed = 0
    for name, ok, detail in results:
        flag = "PASS" if ok else "FAIL"
        print(f"[{flag}] {name}: {detail}")
        passed += ok
    print("=" * 78)
    print(f"通过 {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
