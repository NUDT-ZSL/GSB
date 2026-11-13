#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_paste.py 的本地确定性验收测试（仅标准库）。

运行方式：
    python -I tests\\test_audio_paste.py
或：
    python tests/test_audio_paste.py

测试自身用标准库 wave 生成确定性样本（不访问网络、不依赖第三方库），
通过 subprocess 以命令行方式真实调用 audio_paste.py，并逐项核对 12 条需求。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import wave
from array import array
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "audio_paste.py"
WORK = ROOT / "test_output"

PASS_COUNT = 0
FAIL_COUNT = 0


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def make_wav(path: Path, rate: int, samples, sampwidth: int = 2,
             n_channels: int = 1) -> None:
    """生成确定性 WAV。samples 为有符号整数序列；多声道时按帧交错展开。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    typecode = "h" if sampwidth == 2 else "b"       # 16bit / 8bit
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(array(typecode, samples).tobytes())


def make_ann(path: Path, items, wrapper: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = items if wrapper is None else {**wrapper, "annotations": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        params = {
            "n_channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "framerate": wf.getframerate(),
            "n_frames": wf.getnframes(),
            "duration": wf.getnframes() / wf.getframerate(),
        }
        frames = wf.readframes(wf.getnframes())
    samples = list(array("h", frames)) if params["sampwidth"] == 2 else []
    return params, samples, frames


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """以隔离模式 (-I) 真实调用 CLI，防止工作目录中的伪造模块干扰。"""
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
    )
    proc.stdout_text = proc.stdout.decode("utf-8", errors="replace")
    proc.stderr_text = proc.stderr.decode("utf-8", errors="replace")
    return proc


def base_args(target: Path, source: Path, out_prefix: Path,
              insert: float, target_ann: Path, source_ann: Path | None = None,
              source_start: float | None = None):
    args = ["--target", str(target), "--source", str(source),
            "--insert-point", str(insert),
            "--target-ann", str(target_ann),
            "--output-prefix", str(out_prefix)]
    if source_start is not None:
        args += ["--source-start", str(source_start)]
    if source_ann is not None:
        args += ["--source-ann", str(source_ann)]
    return args


def approx(a, b, eps=1e-12):
    return abs(a - b) <= eps


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def run_test(name, fn):
    global PASS_COUNT, FAIL_COUNT
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 测试框架需要捕获全部异常
        FAIL_COUNT += 1
        print(f"[FAIL] {name}: {exc}")
    else:
        PASS_COUNT += 1
        print(f"[PASS] {name}")


def target_samples(n=40, start=100):
    """目标确定性波形：100,101,..., 与源波形明显不同。"""
    return [start + i for i in range(n)]


def source_samples(n=20):
    """源确定性波形：负值 -200,-201,...，便于字节级比对。"""
    return [-(200 + i) for i in range(n)]


def fresh_dir(name: str) -> Path:
    d = WORK / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


# --------------------------------------------------------------------------- #
# 12 项验收测试
# --------------------------------------------------------------------------- #
def test_01_sample_rate_mismatch():
    d = fresh_dir("t01_rate_mismatch")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 2000, source_samples())          # 采样率不同
    make_ann(d / "t.json", [{"time": 0.01, "label": "x"}])
    out = d / "out" / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode != 0, "采样率不一致时退出码应为非零")
    check("1000" in proc.stderr_text and "2000" in proc.stderr_text,
          f"错误信息应包含两个采样率，实际: {proc.stderr_text!r}")
    check(not (d / "out").exists() or not any((d / "out").iterdir()),
          "采样率不一致时不得生成任何输出文件")


def test_02_annotation_readonly():
    d = fresh_dir("t02_ann_readonly")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    ann = d / "t.json"
    items = [{"time": 0.002, "label": "早"},
             {"time": 0.02, "label": "晚", "confidence": 0.9}]
    make_ann(ann, items)
    before = hashlib.sha256(ann.read_bytes()).hexdigest()
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, ann))
    check(proc.returncode == 0, proc.stderr_text)
    after = hashlib.sha256(ann.read_bytes()).hexdigest()
    check(before == after, "运行后原目标标注文件内容被修改")
    data = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    check(len(data) == 2 and data[0]["label"] == "早", "标注未能正确读取")
    check(data[1].get("confidence") == 0.9, "标注额外字段应保留")


def test_03_paste_duration_and_bytes():
    d = fresh_dir("t03_paste_basic")
    tgt, src = d / "t.wav", d / "s.wav"
    t_s, s_s = target_samples(), source_samples()
    make_wav(tgt, 1000, t_s)
    make_wav(src, 1000, s_s)
    make_ann(d / "t.json", [])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode == 0, proc.stderr_text)
    params, out_samples, out_frames = read_wav(d / "mix_mixed.wav")
    # 时长 = 目标 40ms + 源片段 20ms
    check(params["n_frames"] == 60, f"输出帧数应为 60，实际 {params['n_frames']}")
    check(approx(params["duration"], 0.060), "输出时长应为 0.06s")
    expected = t_s[:8] + s_s + t_s[8:]           # 精确的字节级拼接布局
    check(out_samples == expected, "输出波形字节与预期拼接布局不一致")


def test_04_shift_annotations_after():
    d = fresh_dir("t04_shift_after")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    make_ann(d / "t.json", [
        {"time": 0.020, "label": "later"},       # 帧 20 > 插入帧 8
        {"time": 0.039, "label": "last"},        # 帧 39
    ])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode == 0, proc.stderr_text)
    data = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    got = {x["label"]: x["time"] for x in data}
    # 源片段 20ms：0.020 -> 0.040，0.039 -> 0.059
    check(approx(got["later"], 0.040), f"later 应为 0.040，实际 {got['later']}")
    check(approx(got["last"], 0.059), f"last 应为 0.059，实际 {got['last']}")
    check(all(x["label"] in ("later", "last") for x in data), "标签必须保持不变")


def test_05_keep_annotations_before_and_at():
    d = fresh_dir("t05_keep_before")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    make_ann(d / "t.json", [
        {"time": 0.002, "label": "before"},
        {"time": 0.008, "label": "at-point"},     # 恰好等于插入点
        {"time": 0.0,   "label": "zero"},
    ])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode == 0, proc.stderr_text)
    data = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    got = {x["label"]: x["time"] for x in data}
    check(got["before"] == 0.002 and got["at-point"] == 0.008
          and got["zero"] == 0.0,
          f"插入点及之前的标注必须保持原值，实际 {got}")


def test_06_source_annotations():
    d = fresh_dir("t06_source_ann")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    make_ann(d / "t.json", [{"time": 0.001, "label": "tgt"}])
    # 源时长 20ms；越界条目应被丢弃
    make_ann(d / "s.json", [
        {"time": 0.000, "label": "s0"},
        {"time": 0.010, "label": "s10"},
        {"time": 0.020, "label": "s20-end"},
        {"time": 0.025, "label": "s25-beyond"},
    ])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json",
                              source_ann=d / "s.json"))
    check(proc.returncode == 0, proc.stderr_text)
    data = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    got = {x["label"]: x["time"] for x in data}
    check("s25-beyond" not in got, "超出源片段末尾的源标注必须丢弃")
    check(approx(got["s0"], 0.008), f"s0 应映射到插入点 0.008，实际 {got['s0']}")
    check(approx(got["s10"], 0.018), f"s10 应为 0.018，实际 {got['s10']}")
    check(approx(got["s20-end"], 0.028),
          f"s20-end 应为 0.028，实际 {got['s20-end']}")

    # 带 source-start 的相对偏移：从源第 4 帧起复制 16 帧
    d2 = fresh_dir("t06_source_ann_offset")
    tgt2, src2 = d2 / "t.wav", d2 / "s.wav"
    make_wav(tgt2, 1000, target_samples())
    make_wav(src2, 1000, source_samples())
    make_ann(d2 / "t.json", [])
    make_ann(d2 / "s.json", [
        {"time": 0.003, "label": "before-start"},   # 早于 0.004，丢弃
        {"time": 0.004, "label": "at-start"},       # -> 插入点 0.008
        {"time": 0.012, "label": "mid"},            # 相对 0.008 -> 0.016
        {"time": 0.020, "label": "at-end"},         # 片段末尾 -> 0.024
    ])
    out2 = d2 / "mix"
    proc = run_cli(*base_args(tgt2, src2, out2, 0.008, d2 / "t.json",
                              source_ann=d2 / "s.json", source_start=0.004))
    check(proc.returncode == 0, proc.stderr_text)
    p2, s2, _ = read_wav(d2 / "mix_mixed.wav")
    check(p2["n_frames"] == 40 + 16, "源切片后输出帧数应为 56")
    check(s2 == target_samples()[:8] + source_samples()[4:] + target_samples()[8:],
          "源切片拼接字节布局错误")
    data2 = json.loads((d2 / "mix_annotations.json").read_text(encoding="utf-8"))
    got2 = {x["label"]: x["time"] for x in data2}
    check("before-start" not in got2, "源起始点之前的标注必须丢弃")
    check(approx(got2["at-start"], 0.008) and approx(got2["mid"], 0.016)
          and approx(got2["at-end"], 0.024),
          f"带 source-start 的源标注时间错误: {got2}")


def test_07_append_when_beyond_end():
    d = fresh_dir("t07_append")
    tgt, src = d / "t.wav", d / "s.wav"
    t_s, s_s = target_samples(), source_samples()
    make_wav(tgt, 1000, t_s)
    make_wav(src, 1000, s_s)
    items = [{"time": 0.005, "label": "a"}, {"time": 0.039, "label": "b"}]
    make_ann(d / "t.json", [dict(x) for x in items])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.100, d / "t.json"))  # 远超 40ms
    check(proc.returncode == 0, proc.stderr_text)
    params, out_samples, _ = read_wav(d / "mix_mixed.wav")
    check(params["n_frames"] == 60, "追加模式输出帧数应为目标+源=60")
    check(out_samples == t_s + s_s, "追加模式字节布局应为 目标+源")
    data = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    check([(x["time"], x["label"]) for x in data]
          == [(0.005, "a"), (0.039, "b")],
          "追加模式下所有原标注时间必须保持不变")


def test_08_source_start_beyond_end():
    d = fresh_dir("t08_bad_source_start")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())          # 源时长 20ms
    make_ann(d / "t.json", [])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json",
                              source_start=0.050))
    check(proc.returncode != 0, "源起始点超界时退出码应为非零")
    check("源起始点" in proc.stderr_text,
          f"错误信息应说明源起始点超界，实际: {proc.stderr_text!r}")
    check(not (d / "mix_mixed.wav").exists()
          and not (d / "mix_annotations.json").exists(),
          "报错时不得生成输出文件")
    # 边界：源起始点恰好等于源时长同样非法；差一帧（19ms）合法
    proc_eq = run_cli(*base_args(tgt, src, d / "eq", 0.008, d / "t.json",
                                 source_start=0.020))
    check(proc_eq.returncode != 0, "源起始点恰好等于源时长应报错")
    proc_ok = run_cli(*base_args(tgt, src, d / "ok", 0.008, d / "t.json",
                                 source_start=0.019))
    check(proc_ok.returncode == 0, proc_ok.stderr_text)
    p, _, _ = read_wav(d / "ok_mixed.wav")
    check(p["n_frames"] == 41, "19ms 起切只剩 1 帧，输出应为 41 帧")


def test_09_output_format_matches_target():
    d = fresh_dir("t09_format")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 16000, target_samples())
    make_wav(src, 16000, source_samples())
    make_ann(d / "t.json", [{"time": 0.01, "label": "x"}])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode == 0, proc.stderr_text)
    params, _, _ = read_wav(d / "mix_mixed.wav")
    check(params["framerate"] == 16000, "输出采样率必须与目标一致 (16000Hz)")
    check(params["sampwidth"] == 2, "输出位深必须为 16 位")
    check(params["n_channels"] == 1, "输出必须为单声道")

    # 非单声道 / 非 16 位输入必须被拒绝
    bad_dir = fresh_dir("t09_format_bad")
    st = bad_dir / "stereo.wav"
    make_wav(st, 1000, [1, 2, 3, 4], n_channels=2)
    make_ann(bad_dir / "t.json", [])
    p_st = run_cli(*base_args(st, src, bad_dir / "o1", 0.008,
                              bad_dir / "t.json"))
    check(p_st.returncode != 0 and "单声道" in p_st.stderr_text,
          "立体声文件应被拒绝")
    eight = bad_dir / "eight.wav"
    make_wav(eight, 1000, [1, 2, 3, 4], sampwidth=1)
    p_8 = run_cli(*base_args(eight, src, bad_dir / "o2", 0.008,
                             bad_dir / "t.json"))
    check(p_8.returncode != 0 and "16" in p_8.stderr_text,
          "8 位文件应被拒绝")


def test_10_cli_naming_and_prefix():
    d = fresh_dir("t10_cli")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    make_ann(d / "t.json", [])
    out = d / "nested" / "dir" / "run1"          # 前缀目录不存在也应自动创建
    proc = run_cli(*base_args(tgt, src, out, 0.008, d / "t.json"))
    check(proc.returncode == 0, proc.stderr_text)
    check((d / "nested" / "dir" / "run1_mixed.wav").is_file(),
          "应生成 <前缀>_mixed.wav")
    check((d / "nested" / "dir" / "run1_annotations.json").is_file(),
          "应生成 <前缀>_annotations.json")
    check("完成" in proc.stdout_text, "成功时应在 stdout 打印结果路径")


def test_11_corrupt_inputs_nonzero_exit():
    d = fresh_dir("t11_corrupt")
    good_src = d / "s.wav"
    make_wav(good_src, 1000, source_samples())
    make_ann(d / "t.json", [])

    # 11a. 损坏的 WAV（随机字节，非 RIFF 结构）
    bad_wav = d / "broken.wav"
    bad_wav.write_bytes(b"RIFF\x00\x00not-a-real-wav-file" * 10)
    p1 = run_cli(*base_args(bad_wav, good_src, d / "o1", 0.008, d / "t.json"))
    check(p1.returncode != 0, "损坏 WAV 应返回非零退出码")
    check(p1.stderr_text.strip(), "损坏 WAV 应打印清晰错误信息")
    check(not (d / "o1_mixed.wav").exists(), "失败时不得残留 WAV 输出")

    # 11b. 无效 JSON
    bad_json = d / "broken.json"
    bad_json.write_text("{这不是合法 JSON", encoding="utf-8")
    good_tgt = d / "t.wav"
    make_wav(good_tgt, 1000, target_samples())
    p2 = run_cli(*base_args(good_tgt, good_src, d / "o2", 0.008, bad_json))
    check(p2.returncode != 0, "无效 JSON 应返回非零退出码")
    check("JSON" in p2.stderr_text, f"错误信息应指向 JSON 解析，实际: {p2.stderr_text!r}")
    check(not (d / "o2_annotations.json").exists(), "失败时不得残留标注输出")

    # 11c. 文件不存在
    p3 = run_cli(*base_args(d / "missing.wav", good_src, d / "o3", 0.008,
                            d / "t.json"))
    check(p3.returncode != 0 and p3.stderr_text.strip(),
          "缺失文件应非零退出并打印错误")

    # 11d. 结构非法的标注 JSON（顶层既不是数组也不含 annotations）
    weird = d / "weird.json"
    weird.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    p4 = run_cli(*base_args(good_tgt, good_src, d / "o4", 0.008, weird))
    check(p4.returncode != 0, "结构非法的标注应非零退出")


def test_12_sorting_and_wrapper_format():
    d = fresh_dir("t12_sorting")
    tgt, src = d / "t.wav", d / "s.wav"
    make_wav(tgt, 1000, target_samples())
    make_wav(src, 1000, source_samples())
    # 目标标注：同一时间 0.005 三个标签乱序；插入点 0.010 处一个
    make_ann(d / "t.json", [
        {"time": 0.005, "label": "zebra"},
        {"time": 0.005, "label": "apple"},
        {"time": 0.030, "label": "late"},
        {"time": 0.005, "label": "mango"},
        {"time": 0.010, "label": "at-insert"},
    ], wrapper={"version": 7, "project": "演示"})
    # 源标注：偏移 0 与目标插入点标注同刻，验证跨来源同刻按标签字典序
    make_ann(d / "s.json", [
        {"time": 0.000, "label": "src-zero"},
        {"time": 0.010, "label": "src-mid"},
    ])
    out = d / "mix"
    proc = run_cli(*base_args(tgt, src, out, 0.010, d / "t.json",
                              source_ann=d / "s.json"))
    check(proc.returncode == 0, proc.stderr_text)
    raw = json.loads((d / "mix_annotations.json").read_text(encoding="utf-8"))
    check(isinstance(raw, dict) and raw.get("version") == 7
          and raw.get("project") == "演示",
          "输出 JSON 顶层结构与额外字段必须与原标注文件一致")
    data = raw["annotations"]
    expected = [
        (0.005, "apple"),
        (0.005, "mango"),
        (0.005, "zebra"),
        (0.010, "at-insert"),
        (0.010, "src-zero"),
        (0.020, "src-mid"),
        (0.050, "late"),                # 0.030 + 片段 0.020
    ]
    got = [(x["time"], x["label"]) for x in data]
    check(got == expected, f"排序结果错误:\n  期望 {expected}\n  实际 {got}")
    # 时间键整体非降序
    times = [x["time"] for x in data]
    check(times == sorted(times), "时间序列必须非降序")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
TESTS = [
    ("需求1 采样率不一致报错且无输出", test_01_sample_rate_mismatch),
    ("需求2 正确读取标注且不改原文件", test_02_annotation_readonly),
    ("需求3 粘贴后时长=目标+源片段且字节正确", test_03_paste_duration_and_bytes),
    ("需求4 插入点后标注平移片段时长", test_04_shift_annotations_after),
    ("需求5 插入点及之前标注保持不变", test_05_keep_annotations_before_and_at),
    ("需求6 源标注按相对位置插入(含source-start)", test_06_source_annotations),
    ("需求7 越界插入转追加且原标注不变", test_07_append_when_beyond_end),
    ("需求8 源起始点超界报错且无输出", test_08_source_start_beyond_end),
    ("需求9 输出采样率/位深与目标一致", test_09_output_format_matches_target),
    ("需求10 CLI参数与输出文件命名", test_10_cli_naming_and_prefix),
    ("需求11 损坏WAV/无效JSON清晰报错非零退出", test_11_corrupt_inputs_nonzero_exit),
    ("需求12 标注按时间/标签排序且JSON结构一致", test_12_sorting_and_wrapper_format),
]

if __name__ == "__main__":
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    for name, fn in TESTS:
        run_test(name, fn)
    print(f"\n结果: {PASS_COUNT} 通过 / {FAIL_COUNT} 失败，共 {len(TESTS)} 项")
    sys.exit(1 if FAIL_COUNT else 0)
