#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_acceptance.py —— 播客粗剪工具的本地验收脚本（黑盒，标准库实现）

覆盖任务书 12 条验收点。每次运行先重建确定性素材，再逐用例调用命令行入口。
退出码：0 全部通过；1 存在失败用例。

用法：
  python -I scripts/run_acceptance.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import wave
from array import array
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL = REPO_ROOT / "podcast_editor" / "podcast_editor.py"
GENERATOR = REPO_ROOT / "scripts" / "generate_fixtures.py"
WORK = REPO_ROOT / "acceptance_work"
FIXTURES = WORK / "fixtures"
RUNS = WORK / "runs"

TARGET_PEAK = 20000
SAMPLE_MIN = -(2 ** 15)
SAMPLE_MAX = 2 ** 15 - 1

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    return ok


def skip(name: str, detail: str) -> None:
    results.append((SKIP, name, detail))


def run_tool(*cli_args: str) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-I", str(TOOL), *cli_args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_generator() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(GENERATOR), "--out", str(FIXTURES)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def read_wav(path: Path):
    with wave.open(str(path), "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return params, samples


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def case_dir(name: str) -> Path:
    d = RUNS / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(name: str) -> Path:
    return FIXTURES / "manifests" / name


def main_wav() -> Path:
    return FIXTURES / "audio" / "main.wav"


def base_args(out_dir: Path, manifest: Path, peak: int = TARGET_PEAK) -> list[str]:
    return ["--manifest", str(manifest), "--main", str(main_wav()),
            "--output-dir", str(out_dir), "--target-peak", str(peak)]


def expect_rejected(name: str, cli_args: list[str], expected_text: str,
                    out_dir: Path | None = None) -> None:
    proc = run_tool(*cli_args)
    stderr = proc.stderr or ""
    ok = proc.returncode != 0 and expected_text in stderr
    if out_dir is not None and out_dir.exists():
        wav_file = out_dir / "podcast_output.wav"
        report_file = out_dir / "processing_report.json"
        ok = ok and not wav_file.exists() and not report_file.exists()
    record(name, ok,
           f"exit={proc.returncode}, stderr包含'{expected_text}'={'是' if expected_text in stderr else '否'}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if WORK.exists():
        shutil.rmtree(WORK)
    gen = run_generator()
    if gen.returncode != 0:
        print("素材生成失败：", gen.stdout, gen.stderr)
        return 1

    main_params, main_samples = read_wav(main_wav())
    main_rate = main_params.framerate
    main_frames = main_params.nframes
    main_duration = main_frames / main_rate
    print(f"主干: {main_rate}Hz, {main_params.sampwidth * 8}-bit, {main_params.nchannels}ch, "
          f"{main_frames} samples, {main_duration:.3f}s")

    # ---------------------------------------------------------------- #
    # 验收 1：重复 clip_id -> 非零退出并提示重复标识
    # ---------------------------------------------------------------- #
    d = case_dir("t01_dup_id")
    expect_rejected(
        "T01 重复 clip_id 整体拒绝并提示重复标识",
        base_args(d, manifest_path("manifest_dup_id.json")),
        "clip_id 重复", d,
    )

    # ---------------------------------------------------------------- #
    # 验收 2 + 4：正常清单；成品时长 = 主干 - 删除 + 提示音；
    # 22050Hz 提示音 -> 成品 44100Hz/16bit/单声道且无越界样本
    # ---------------------------------------------------------------- #
    d = case_dir("t02_valid")
    proc = run_tool(*base_args(d, manifest_path("manifest_valid.json")))
    wav_file = d / "podcast_output.wav"
    report_file = d / "processing_report.json"
    if proc.returncode == 0 and wav_file.exists() and report_file.exists():
        params, samples = read_wav(wav_file)
        report = json.loads(report_file.read_text(encoding="utf-8"))

        tone_b_native = read_wav(FIXTURES / "audio" / "tone_b_22050.wav")[0]
        # 预期时长：12 - (1+1) + 1.0(tone_a) + 重采样后 tone_b 的时长
        tone_b_resampled_sec = round(tone_b_native.nframes / tone_b_native.framerate, 6)
        expected_duration = main_duration - 2.0 + 1.0 + tone_b_resampled_sec
        actual_duration = len(samples) / params.framerate

        ok_rate = params.framerate == main_rate == 44100
        ok_width = params.sampwidth == 2 and params.nchannels == 1
        ok_range = all(SAMPLE_MIN <= v <= SAMPLE_MAX for v in samples)
        ok_dur = abs(actual_duration - expected_duration) < 1e-9
        ok_all_processed = report["summary"]["unprocessed"] == 0
        record(
            "T02 成品时长=主干-删除+提示音，且采样率/位深/声道与主干一致、无越界样本",
            ok_rate and ok_width and ok_range and ok_dur and ok_all_processed,
            f"rate={params.framerate}(ok={ok_rate}), 16bit/mono={ok_width}, "
            f"无越界={ok_range}, 时长={actual_duration:.6f}s 预期={expected_duration:.6f}s "
            f"匹配={ok_dur}, 全部成功={ok_all_processed}",
        )
    else:
        record("T02 正常清单处理", False, f"exit={proc.returncode}, stderr={proc.stderr.strip()}")

    # ---------------------------------------------------------------- #
    # 验收 3 + 7：起止倒置/越界片段未处理，其余成功；成功片段不丢失
    # ---------------------------------------------------------------- #
    d = case_dir("t03_bad_clip")
    proc = run_tool(*base_args(d, manifest_path("manifest_bad_clip.json")))
    report_file = d / "processing_report.json"
    if proc.returncode == 0 and report_file.exists():
        report = json.loads(report_file.read_text(encoding="utf-8"))
        by_id = {c["clip_id"]: c for c in report["clips"]}
        ok_unprocessed = (
            by_id["b02-reversed"]["status"] == "unprocessed"
            and "起点不早于终点" in (by_id["b02-reversed"]["reason"] or "")
            and by_id["b03-out-of-range"]["status"] == "unprocessed"
            and "越界" in (by_id["b03-out-of-range"]["reason"] or "")
        )
        ok_others = (
            by_id["b01-delete-ok"]["status"] == "processed"
            and by_id["b04-insert-ok"]["status"] == "processed"
        )
        # 成品时长：12 - 1(仅 b01 删除生效) + 1(tone_a)；两个坏删除不改变素材
        _, samples = read_wav(d / "podcast_output.wav")
        expected = main_duration - 1.0 + 1.0
        ok_dur = abs(len(samples) / main_rate - expected) < 1e-9
        record(
            "T03 坏片段标记未处理且不影响其余片段，报告保留全部片段",
            ok_unprocessed and ok_others and ok_dur,
            f"坏片段未处理={ok_unprocessed}, 其余成功={ok_others}, "
            f"时长={len(samples) / main_rate:.3f}s 预期={expected:.3f}s",
        )
    else:
        record("T03 含坏片段清单处理", False, f"exit={proc.returncode}, stderr={proc.stderr.strip()}")

    # ---------------------------------------------------------------- #
    # 验收 5：非法目标峰值拒绝；合法峰值下最大幅值不超过目标
    # ---------------------------------------------------------------- #
    d = case_dir("t05_bad_peak")
    expect_rejected(
        "T05a 非法目标峰值（>32767）非零退出",
        base_args(d, manifest_path("manifest_valid.json"), peak=99999),
        "目标峰值非法", d,
    )
    d = case_dir("t05_bad_peak_zero")
    proc = run_tool(*base_args(d, manifest_path("manifest_valid.json"), peak=0))
    record("T05b 非法目标峰值（0）非零退出", proc.returncode != 0 and "目标峰值非法" in (proc.stderr or ""),
           f"exit={proc.returncode}")

    d = case_dir("t05_good_peak")
    proc = run_tool(*base_args(d, manifest_path("manifest_valid.json"), peak=TARGET_PEAK))
    if proc.returncode == 0:
        _, samples = read_wav(d / "podcast_output.wav")
        measured = max(abs(v) for v in samples)
        record("T05c 合法峰值下成品最大幅值不超过目标", measured <= TARGET_PEAK,
               f"measured_peak={measured}, target={TARGET_PEAK}")
    else:
        record("T05c 合法峰值处理", False, proc.stderr.strip())

    # ---------------------------------------------------------------- #
    # 验收 6：重复运行同名成品已存在 -> 拒绝且原文件不变
    # ---------------------------------------------------------------- #
    d = case_dir("t06_rerun")
    first = run_tool(*base_args(d, manifest_path("manifest_valid.json")))
    wav_file = d / "podcast_output.wav"
    if first.returncode == 0:
        before = sha256_of(wav_file)
        second = run_tool(*base_args(d, manifest_path("manifest_valid.json")))
        after = sha256_of(wav_file)
        ok = (second.returncode != 0
              and "已存在" in (second.stderr or "")
              and before == after)
        record("T06 同名成品已存在时拒绝且不覆盖原文件", ok,
               f"第二次exit={second.returncode}, 原文件哈希不变={before == after}")
    else:
        record("T06 首次运行", False, first.stderr.strip())

    # 输出目录不可写（POSIX 只读位；Windows 下文件只读与目录只读语义不同，
    # 因此用一个已存在的普通文件作为输出路径，跨平台稳定触发拒绝）
    blocked = RUNS / "t06_blocked_file"
    blocked.write_text("not a directory", encoding="utf-8")
    proc = run_tool(*base_args(blocked, manifest_path("manifest_valid.json")))
    record("T06b 输出路径不可用（被文件占用）时拒绝",
           proc.returncode != 0 and "不是目录" in (proc.stderr or ""),
           f"exit={proc.returncode}")

    # 真实只读目录：chmod 去写位后写入探针必须失败（测试结束恢复权限以便清理）。
    # Windows 的目录只读位不阻止在其中创建文件，语义不同，故该用例仅 POSIX 执行；
    # Windows 不可写场景由 T06b（路径被文件占用）覆盖。
    ro_dir = case_dir("t06_readonly")
    if os.name != "posix":
        skip("T06c 只读输出目录探针",
             "当前平台目录只读位不阻止内部创建文件，Windows 场景由 T06b 覆盖")
    else:
        try:
            ro_dir.chmod(0o555)
            proc = run_tool(*base_args(ro_dir, manifest_path("manifest_valid.json")))
            record("T06c 只读输出目录被识别为不可写且无产物",
                   proc.returncode != 0 and "不可写" in (proc.stderr or "")
                   and not (ro_dir / "podcast_output.wav").exists()
                   and not (ro_dir / "processing_report.json").exists(),
                   f"exit={proc.returncode}, stderr={proc.stderr.strip()}")
        finally:
            ro_dir.chmod(0o755)

    # ---------------------------------------------------------------- #
    # 验收 8：缺字段 / 非法 JSON -> 整体拒绝且无任何产物
    # ---------------------------------------------------------------- #
    d = case_dir("t08_missing_field")
    expect_rejected(
        "T08a 缺少必需字段整体拒绝且不生成产物",
        base_args(d, manifest_path("manifest_missing_field.json")),
        "缺少必需字段", d,
    )
    d = case_dir("t08_bad_json")
    expect_rejected(
        "T08b 非法 JSON 整体拒绝且不生成产物",
        base_args(d, manifest_path("manifest_bad_json.json")),
        "JSON 格式错误", d,
    )

    # ---------------------------------------------------------------- #
    # 验收 9：主干不存在 -> 非零退出，错误信息含路径，无产物
    # ---------------------------------------------------------------- #
    d = case_dir("t09_missing_main")
    missing_main = FIXTURES / "audio" / "does_not_exist.wav"
    args = ["--manifest", str(manifest_path("manifest_valid.json")),
            "--main", str(missing_main), "--output-dir", str(d),
            "--target-peak", str(TARGET_PEAK)]
    proc = run_tool(*args)
    ok = (proc.returncode != 0 and str(missing_main) in (proc.stderr or "")
          and not (d / "podcast_output.wav").exists()
          and not (d / "processing_report.json").exists())
    record("T09 主干缺失整体拒绝，错误信息包含该路径且无产物", ok,
           f"exit={proc.returncode}, stderr={proc.stderr.strip()}")

    # ---------------------------------------------------------------- #
    # 验收 10：多主干 / 绝对路径提示音 -> 整体拒绝并说明原因
    # ---------------------------------------------------------------- #
    d = case_dir("t10_multi_main")
    expect_rejected(
        "T10a 清单引用多个主干整体拒绝",
        base_args(d, manifest_path("manifest_multi_main.json")),
        "一个主干", d,
    )
    d = case_dir("t10_abs_tone")
    expect_rejected(
        "T10b 绝对路径提示音整体拒绝并说明原因",
        base_args(d, manifest_path("manifest_abs_tone.json")),
        "绝对路径", d,
    )

    # ---------------------------------------------------------------- #
    # 验收 11：按报告排序说明可还原清单原始顺序
    # ---------------------------------------------------------------- #
    d = case_dir("t11_ordering")
    proc = run_tool(*base_args(d, manifest_path("manifest_valid.json")))
    if proc.returncode == 0:
        manifest = json.loads(manifest_path("manifest_valid.json").read_text(encoding="utf-8"))
        report = json.loads((d / "processing_report.json").read_text(encoding="utf-8"))
        rule = report.get("ordering", {})
        restored = sorted(report["clips"], key=lambda c: (c["manifest_index"], c["clip_id"]))
        ok = (rule.get("field") == "manifest_index"
              and [c["clip_id"] for c in restored]
              == [c["clip_id"] for c in manifest["clips"]])
        record("T11 按报告 ordering 说明（manifest_index 升序）可还原清单原始顺序", ok,
               f"还原顺序={[c['clip_id'] for c in restored]}")
    else:
        record("T11 排序依据验证", False, proc.stderr.strip())

    # ---------------------------------------------------------------- #
    # 验收 12：无参数输出用法并非零退出；相同输入两次运行成品字节一致
    # ---------------------------------------------------------------- #
    proc = run_tool()
    ok = proc.returncode != 0 and "usage:" in (proc.stderr or "")
    record("T12a 无参数运行输出用法并非零退出", ok,
           f"exit={proc.returncode}, usage出现={ok}")

    d1 = case_dir("t12_determinism_a")
    d2 = case_dir("t12_determinism_b")
    p1 = run_tool(*base_args(d1, manifest_path("manifest_valid.json")))
    p2 = run_tool(*base_args(d2, manifest_path("manifest_valid.json")))
    if p1.returncode == 0 and p2.returncode == 0:
        h1 = sha256_of(d1 / "podcast_output.wav")
        h2 = sha256_of(d2 / "podcast_output.wav")
        # 报告含 resolved_path（不同输出目录），归一化路径字段后报告内容应完全一致
        rep1 = json.loads((d1 / "processing_report.json").read_text(encoding="utf-8"))
        rep2 = json.loads((d2 / "processing_report.json").read_text(encoding="utf-8"))
        rep1["main_recording"]["resolved_path"] = "<normalized>"
        rep2["main_recording"]["resolved_path"] = "<normalized>"
        record("T12b 相同输入与参数两次运行成品字节完全一致",
               h1 == h2, f"wav sha256 相同={h1 == h2}")
        record("T12c 报告内容确定性（归一化输出路径后两份报告一致）",
               rep1 == rep2, f"报告字典归一化后相等={rep1 == rep2}")
    else:
        record("T12b 确定性验证", False, f"{p1.stderr} {p2.stderr}")

    # ---------------------------------------------------------------- #
    # 汇总
    # ---------------------------------------------------------------- #
    print("\n================ 验收结果 ================")
    fail_count = 0
    skip_count = 0
    for status, name, detail in results:
        if status == FAIL:
            fail_count += 1
        elif status == SKIP:
            skip_count += 1
        print(f"[{status}] {name}")
        print(f"       {detail}")
    print("==========================================")
    total = len(results)
    print(f"合计 {total} 项，通过 {total - fail_count - skip_count} 项，"
          f"失败 {fail_count} 项，跳过 {skip_count} 项。")
    print(f"工作目录: {WORK}")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
