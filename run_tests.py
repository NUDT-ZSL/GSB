#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""podcast_editor.py 的端到端验收脚本 (对应任务 15 条验收点)。

运行方式:
    python -I run_tests.py

脚本会:
  1. 调用 make_samples.py 生成确定性 WAV 样本;
  2. 在 tests_work/ 下动态生成编辑脚本与输出;
  3. 以子进程方式运行命令行工具并逐条核对验收点;
  4. 打印每条 PASS/FAIL，全部通过退出码 0，否则 1。
"""

from __future__ import annotations

import array
import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")
WORK = os.path.join(ROOT, "tests_work")
EDITOR = os.path.join(ROOT, "podcast_editor.py")
MAKE_SAMPLES = os.path.join(ROOT, "make_samples.py")

SINE = os.path.join(SAMPLES, "source_16k_mono.wav")
SQUARE = os.path.join(SAMPLES, "square_16k_mono.wav")
LOUD = os.path.join(SAMPLES, "loud_16k_mono.wav")
STEREO = os.path.join(SAMPLES, "stereo_16k.wav")


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------
def _decode(data: bytes) -> str:
    """子进程输出解码: 优先 UTF-8，回退到中文 Windows 常用的 GBK。"""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_editor(*args: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-I", EDITOR, *args],
        capture_output=True,
    )
    return subprocess.CompletedProcess(
        proc.args, proc.returncode, _decode(proc.stdout), _decode(proc.stderr)
    )


def write_script(name: str, payload: dict) -> str:
    path = os.path.join(WORK, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def out_paths(tag: str) -> tuple[str, str]:
    return (
        os.path.join(WORK, f"{tag}.wav"),
        os.path.join(WORK, f"{tag}_report.json"),
    )


def read_wav(path: str) -> tuple[array.array, int, int, int]:
    with wave.open(path, "rb") as wf:
        nch, sw, sr, nf = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        raw = wf.readframes(nf)
    samples = array.array("h")
    samples.frombytes(raw)
    return samples, nch, sw, sr


def read_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            h.update(block)
    return h.hexdigest()


results: list[tuple[str, bool, str]] = []


def check(code: str, title: str, ok: bool, detail: str = "") -> None:
    results.append((code, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {code} {title}" + (f" -- {detail}" if detail else ""))


def expect_success(tag: str, script_payload: dict, wav_in: str = SQUARE):
    script = write_script(f"{tag}_script.json", script_payload)
    wav_out, report_out = out_paths(tag)
    proc = run_editor(wav_in, script, wav_out, report_out)
    assert proc.returncode == 0, f"{tag} 应成功退出, 实际 {proc.returncode}: {proc.stderr}"
    return wav_out, report_out


# ----------------------------------------------------------------------
# 验收点
# ----------------------------------------------------------------------
def t01_no_args() -> None:
    """1. 缺参打印用法、非零退出、不创建任何文件。"""
    empty_dir = os.path.join(WORK, "t01_empty")
    os.makedirs(empty_dir, exist_ok=True)
    before = set(os.listdir(empty_dir))
    proc = run_editor()  # 无参数
    after = set(os.listdir(empty_dir))
    ok = (
        proc.returncode != 0
        and "用法" in proc.stderr
        and before == after
    )
    check("T01", "缺参: 非零码+用法说明+无新增文件", ok,
          f"rc={proc.returncode}, 新增文件={sorted(after - before)}")


def t02_basic_concat() -> None:
    """2. 两段合法片段: 输出时长 = 两段之和。"""
    wav_out, report = expect_success("t02", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": None, "fade_out_ms": None},
            {"start_ms": 1000, "end_ms": 1500, "fade_in_ms": None, "fade_out_ms": None},
        ],
    })
    samples, nch, sw, sr = read_wav(wav_out)
    rep = read_report(report)
    ok = len(samples) == 16000 and rep["total_frames"] == 16000 and rep["accepted_count"] == 2
    check("T02", "两合法片段拼接时长=时长之和 (1.0s/16000帧)", ok,
          f"frames={len(samples)}, accepted={rep['accepted_count']}")


def t03_fade_in_out() -> None:
    """3. 100ms 淡入: 开头增益由 0 线性升到 1；附带核对淡出。"""
    wav_out, _ = expect_success("t03", {
        "volume": 100,
        "segments": [
            {"start_ms": 1000, "end_ms": 2000, "fade_in_ms": 100, "fade_out_ms": 100},
        ],
    })
    out, *_ = read_wav(wav_out)
    src, *_ = read_wav(SQUARE)
    base = src[16000:32000]  # 对应片段 [1000ms, 2000ms)
    fade_frames = 1600  # 100ms * 16kHz
    ok = out[0] == 0
    detail = [f"out[0]={out[0]}"]
    # 淡入区间逐样本核对线性增益 i/1600 (与工具同一运算顺序)
    for i in range(fade_frames):
        expected = int(round(base[i] * (i / fade_frames)))
        if out[i] != expected:
            ok = False
            detail.append(f"淡入偏差@{i}: {out[i]}!={expected}")
            break
    # 淡入结束后恢复原值 (无放大)
    if out[fade_frames] != base[fade_frames]:
        ok = False
        detail.append(f"淡入结束帧未恢复原值: {out[fade_frames]}!={base[fade_frames]}")
    # 淡出: 末帧为 0，区间线性
    if out[-1] != 0:
        ok = False
        detail.append(f"淡出末帧非0: {out[-1]}")
    n = len(out)
    for i in range(fade_frames):
        gain = i / fade_frames
        pos = n - 1 - i
        expected = int(round(base[pos] * gain))
        if out[pos] != expected:
            ok = False
            detail.append(f"淡出偏差@{pos}: {out[pos]}!={expected}")
            break
    check("T03", "100ms 淡入从零线性升至原值, 淡出对称归零", ok, "; ".join(detail[:3]))


def t04_invalid_volume() -> None:
    """4. 档位 500 非法: 按默认 100 导出，报告标注非法。"""
    payload = {
        "volume": 500,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": None, "fade_out_ms": None},
        ],
    }
    wav_bad, rep_bad = expect_success("t04_bad", payload)
    payload["volume"] = 100
    wav_ok, rep_ok = expect_success("t04_ok", payload)
    rep = read_report(rep_bad)
    ok = (
        sha256(wav_bad) == sha256(wav_ok)
        and rep["volume"]["valid"] is False
        and rep["volume"]["applied"] == 100
        and "音量档位非法" in rep["volume"]["reason"]
    )
    check("T04", "非法档位500回退默认100且与默认输出一致, 报告标注", ok,
          f"report.volume={rep['volume']}")


def t05_mixed_reject() -> None:
    """5. 越界片段被拒绝，合法片段照常，总时长正确。"""
    wav_out, report = expect_success("t05", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": None, "fade_out_ms": None},
            {"start_ms": 3500, "end_ms": 5000, "fade_in_ms": None, "fade_out_ms": None},  # 越界
            {"start_ms": 1500, "end_ms": 2000, "fade_in_ms": None, "fade_out_ms": None},
        ],
    })
    samples, *_ = read_wav(wav_out)
    rep = read_report(report)
    rejected = rep["rejected_segments"]
    ok = (
        len(samples) == 16000
        and len(rejected) == 1 and rejected[0]["index"] == 1
        and "超出" in rejected[0]["reason"]
        and rep["accepted_count"] == 2
    )
    check("T05", "越界片段拒绝、其余正常, 总时长 1.0s", ok,
          f"frames={len(samples)}, rejected={rejected}")


def t06_fade_overflow() -> None:
    """6. 淡变合计 == 片段时长: 拒绝且不进输出。"""
    wav_out, report = expect_success("t06", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": 250, "fade_out_ms": 250},
        ],
    })
    samples, *_ = read_wav(wav_out)
    rep = read_report(report)
    rejected = rep["rejected_segments"]
    ok = (
        len(samples) == 0
        and len(rejected) == 1 and rejected[0]["index"] == 0
        and rep["accepted_count"] == 0
    )
    check("T06", "淡变合计=片段时长 -> 拒绝, 输出为空", ok,
          f"rejected={rejected}")


def t07_exact_concat() -> None:
    """7. 精确拼接无静音/重叠, 采样率声道与输入一致。"""
    wav_out, _ = expect_success("t07", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": None, "fade_out_ms": None},
            {"start_ms": 1000, "end_ms": 1500, "fade_in_ms": None, "fade_out_ms": None},
        ],
    })
    out, nch, sw, sr = read_wav(wav_out)
    src, *_ = read_wav(SQUARE)
    expected = array.array("h", src[0:8000])
    expected.extend(src[16000:24000])
    ok = (
        len(out) == 16000 and nch == 1 and sw == 2 and sr == 16000
        and out == expected
    )
    check("T07", "两段0.5s精确拼接=1.0s, 16kHz/单声道/16bit, 无静音重叠", ok,
          f"frames={len(out)}, ch={nch}, sr={sr}, 样本逐帧一致={out == expected}")


def t08_report_order() -> None:
    """8. 报告成功/拒绝列表均按序号升序。"""
    wav_out, report = expect_success("t08", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 200, "fade_in_ms": None, "fade_out_ms": None},      # 0 成功
            {"start_ms": 9000, "end_ms": 9999, "fade_in_ms": None, "fade_out_ms": None},  # 1 越界拒绝
            {"start_ms": 500, "end_ms": 700, "fade_in_ms": None, "fade_out_ms": None},    # 2 成功
            {"start_ms": 100, "end_ms": 50, "fade_in_ms": None, "fade_out_ms": None},     # 3 颠倒拒绝
            {"start_ms": 700, "end_ms": 900, "fade_in_ms": None, "fade_out_ms": None},    # 4 成功
        ],
    })
    rep = read_report(report)
    acc_idx = [s["index"] for s in rep["accepted_segments"]]
    rej_idx = [s["index"] for s in rep["rejected_segments"]]
    samples, *_ = read_wav(wav_out)
    accepted_frames = sum(s["frames"] for s in rep["accepted_segments"])
    ok = (
        acc_idx == sorted(acc_idx) == [0, 2, 4]
        and rej_idx == sorted(rej_idx) == [1, 3]
        and accepted_frames == len(samples) == rep["total_frames"]
    )
    check("T08", "报告两列表按序号升序, 成功列表与输出帧数对应", ok,
          f"accepted={acc_idx}, rejected={rej_idx}")


def t09_missing_input() -> None:
    """9. 输入 WAV 不存在: 非零码, 错误含文件名, 不生成输出。"""
    missing = os.path.join(WORK, "no_such_input.wav")
    script = write_script("t09_script.json", {"volume": 100, "segments": []})
    wav_out, report_out = out_paths("t09")
    proc = run_editor(missing, script, wav_out, report_out)
    ok = (
        proc.returncode != 0
        and os.path.basename(missing) in proc.stderr
        and not os.path.exists(wav_out)
        and not os.path.exists(report_out)
    )
    check("T09", "输入缺失: 非零码+错误含文件名+无输出文件", ok,
          f"rc={proc.returncode}, stderr={proc.stderr.strip()[:80]}")


def t10_stereo() -> None:
    """10. 立体声 WAV: 拒绝并点名声道不兼容, 无输出。"""
    script = write_script("t10_script.json", {"volume": 100, "segments": []})
    wav_out, report_out = out_paths("t10")
    proc = run_editor(STEREO, script, wav_out, report_out)
    ok = (
        proc.returncode != 0
        and "声道" in proc.stderr
        and not os.path.exists(wav_out)
        and not os.path.exists(report_out)
    )
    check("T10", "立体声输入: 非零码+提示声道不兼容+无输出", ok,
          f"rc={proc.returncode}, stderr={proc.stderr.strip()[:80]}")


def t11_bad_json() -> None:
    """11. 截断 JSON: 非零码, 不留 WAV 或报告。"""
    script = os.path.join(WORK, "t11_broken.json")
    with open(script, "w", encoding="utf-8", newline="\n") as fh:
        fh.write('{"volume": 100, "segments": [ {"start_ms": 0, "end_')  # 截断
    wav_out, report_out = out_paths("t11")
    proc = run_editor(SQUARE, script, wav_out, report_out)
    ok = (
        proc.returncode != 0
        and "JSON" in proc.stderr
        and not os.path.exists(wav_out)
        and not os.path.exists(report_out)
    )
    check("T11", "截断JSON: 非零码+可读错误+无残缺输出", ok,
          f"rc={proc.returncode}, stderr={proc.stderr.strip()[:80]}")


def t12_wav_integrity() -> None:
    """12. 输出 WAV 可被 wave 读取, getnframes 与报告总采样数一致。"""
    wav_out, report = expect_success("t12", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 333, "fade_in_ms": 20, "fade_out_ms": None},
            {"start_ms": 2000, "end_ms": 2777, "fade_in_ms": None, "fade_out_ms": 50},
        ],
    })
    with wave.open(wav_out, "rb") as wf:
        nframes = wf.getnframes()
        header_bytes = wf.getnframes() * wf.getnchannels() * wf.getsampwidth()
    actual_data_bytes = os.path.getsize(wav_out) - 44  # 标准 PCM WAV 头 44 字节
    rep = read_report(report)
    ok = (
        nframes == rep["total_frames"]
        and header_bytes == actual_data_bytes
    )
    check("T12", "WAV头帧数=报告总采样数, 无截断/填充偏差", ok,
          f"getnframes={nframes}, report={rep['total_frames']}, 数据区字节={actual_data_bytes}")


def t13_volume_clamp() -> None:
    """13. 高合法档位缩放后钳制到边界, 无负值回绕。"""
    wav_out, report = expect_success("t13", {
        "volume": 200,
        "segments": [
            {"start_ms": 0, "end_ms": 1000, "fade_in_ms": None, "fade_out_ms": None},
        ],
    }, wav_in=LOUD)
    samples, *_ = read_wav(wav_out)
    peak, valley = max(samples), min(samples)
    ok = (
        peak == 32767
        and valley == -32768
        and all(-32768 <= v <= 32767 for v in samples)
    )
    check("T13", "200%档位: 峰值钳制32767/谷值-32768, 无回绕", ok,
          f"peak={peak}, valley={valley}")


def t14_partial_then_error() -> None:
    """14. 前段合法后段越界: 已导出部分保留, 报告列拒绝原因。"""
    wav_out, report = expect_success("t14", {
        "volume": 100,
        "segments": [
            {"start_ms": 0, "end_ms": 500, "fade_in_ms": None, "fade_out_ms": None},
            {"start_ms": 3800, "end_ms": 4600, "fade_in_ms": None, "fade_out_ms": None},  # 越界
        ],
    })
    out, *_ = read_wav(wav_out)
    src, *_ = read_wav(SQUARE)
    rep = read_report(report)
    ok = (
        len(out) == 8000
        and out == array.array("h", src[0:8000])
        and [r["index"] for r in rep["rejected_segments"]] == [1]
    )
    check("T14", "后段越界不污染前段: 输出保留前段, 报告记录拒绝", ok,
          f"frames={len(out)}, rejected={[r['index'] for r in rep['rejected_segments']]}")


def t15_deterministic() -> None:
    """15. 同输入同脚本重复运行: WAV 与报告逐字节一致。"""
    payload = {
        "volume": 120,
        "segments": [
            {"start_ms": 100, "end_ms": 600, "fade_in_ms": 50, "fade_out_ms": None},
            {"start_ms": 2000, "end_ms": 2500, "fade_in_ms": None, "fade_out_ms": 80},
            {"start_ms": 3900, "end_ms": 4100, "fade_in_ms": None, "fade_out_ms": None},
        ],
    }
    script = write_script("t15_script.json", payload)
    wav1, rep1 = out_paths("t15_run1")
    wav2, rep2 = out_paths("t15_run2")
    p1 = run_editor(SINE, script, wav1, rep1)
    p2 = run_editor(SINE, script, wav2, rep2)
    ok = (
        p1.returncode == 0 and p2.returncode == 0
        and sha256(wav1) == sha256(wav2)
        and sha256(rep1) == sha256(rep2)
    )
    check("T15", "两次运行 WAV/报告哈希一致 (可复现)", ok,
          f"wav={sha256(wav1)[:12]}, report={sha256(rep1)[:12]}")


def main() -> int:
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK, exist_ok=True)

    # 1. 确定性样本
    gen = subprocess.run([sys.executable, "-I", MAKE_SAMPLES, SAMPLES],
                         capture_output=True)
    if gen.returncode != 0:
        print("样本生成失败:", _decode(gen.stderr))
        return 1
    print(_decode(gen.stdout).strip(), "\n")

    # 2. 逐条验收
    for test in (
        t01_no_args, t02_basic_concat, t03_fade_in_out, t04_invalid_volume,
        t05_mixed_reject, t06_fade_overflow, t07_exact_concat, t08_report_order,
        t09_missing_input, t10_stereo, t11_bad_json, t12_wav_integrity,
        t13_volume_clamp, t14_partial_then_error, t15_deterministic,
    ):
        test()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n==== 验收结果: {passed}/{len(results)} 通过 ====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
