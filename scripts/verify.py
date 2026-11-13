# -*- coding: utf-8 -*-
"""端到端验收脚本：覆盖任务全部 8 条验收标准。

流程：
  1. 在 verify_work/ 下用 make_samples.py 生成确定性 WAV 样本；
  2. 为每个场景写 JSON 配置，以子进程方式调用 wav_editor（真实 CLI）；
  3. 用标准库 wave / json 检查输出 WAV、报告结构、退出码与控制台信息。

任何一项断言失败则脚本以退出码 1 结束；全部通过则退出码 0。

用法（在项目根目录执行）：
  python scripts/verify.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "verify_work")
SAMPLES = os.path.join(WORK, "samples")
CONFIGS = os.path.join(WORK, "configs")
OUT = os.path.join(WORK, "outputs")

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, bool(condition), detail))
    flag = "PASS" if condition else "FAIL"
    print("[%s] %s %s" % (flag, name, ("- " + detail) if detail else ""))


def run_editor(case: str, config: dict) -> subprocess.CompletedProcess:
    cfg_dir = os.path.join(CONFIGS, case)
    out_dir = os.path.join(OUT, case)
    os.makedirs(cfg_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    cfg_path = os.path.join(cfg_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    out_wav = os.path.join(out_dir, "program.wav")
    report = os.path.join(out_dir, "report.json")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "podcast_editor.wav_editor",
            "--config",
            cfg_path,
            "--output",
            out_wav,
            "--report",
            report,
        ],
        cwd=ROOT,
        capture_output=True,
    )
    proc.out_wav = out_wav  # type: ignore[attr-defined]
    proc.report = report  # type: ignore[attr-defined]
    return proc


def load_report(proc: subprocess.CompletedProcess) -> dict:
    with open(proc.report, "r", encoding="utf-8") as fh:
        return json.load(fh)


def wav_info(path: str) -> tuple[int, int, int, int]:
    with wave.open(path, "rb") as wf:
        return wf.getnchannels(), wf.getframerate(), wf.getsampwidth(), wf.getnframes()


def assert_reasons_nonempty(report: dict) -> bool:
    for key in ("complete_segments", "partial_segments", "failed_segments"):
        for entry in report[key]:
            if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
                return False
            if "index" not in entry:
                return False
    return True


def main() -> int:
    # ---- 0. 准备干净工作区与确定性样本 -------------------------------- #
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    os.makedirs(SAMPLES)
    gen = subprocess.run(
        [sys.executable, os.path.join("scripts", "make_samples.py"), "--out", SAMPLES],
        cwd=ROOT,
        capture_output=True,
    )
    check("0. 样本生成成功", gen.returncode == 0, gen.stderr.decode("utf-8", "replace"))

    a = os.path.join(SAMPLES, "a.wav")
    b = os.path.join(SAMPLES, "b.wav")
    c = os.path.join(SAMPLES, "c.wav")
    diff = os.path.join(SAMPLES, "diff.wav")
    corrupt = os.path.join(SAMPLES, "corrupt.wav")
    ghost = os.path.join(SAMPLES, "missing_src.wav")

    # 缺失文件场景目录：复制 a/b/c 后删除 b
    miss_dir = os.path.join(WORK, "case_missing")
    os.makedirs(miss_dir, exist_ok=True)
    for name in ("a.wav", "b.wav", "c.wav"):
        shutil.copy2(os.path.join(SAMPLES, name), os.path.join(miss_dir, name))
    os.remove(os.path.join(miss_dir, "b.wav"))
    miss_a, miss_b, miss_c = (
        os.path.join(miss_dir, "a.wav"),
        os.path.join(miss_dir, "b.wav"),
        os.path.join(miss_dir, "c.wav"),
    )

    # ---- 验收 1：合法配置，总时长 = 各片段有效时长之和 ---------------- #
    cfg1 = {
        "clips": [
            {"source": a, "start": 0.0, "end": 2.0},       # 2.0s
            {"source": b, "start": 0.5, "end": 2.0},       # 1.5s
            {"source": c, "start": 1.0, "end": 4.0},       # 3.0s
        ]
    }
    proc = run_editor("t1_normal", cfg1)
    check("1. 正常场景退出码为 0", proc.returncode == 0)
    report = load_report(proc)
    ch, rate, width, frames = wav_info(proc.out_wav)
    expected_frames = int(2.0 * 8000) + int(1.5 * 8000) + int(3.0 * 8000)
    check(
        "1. 输出总时长等于各片段有效时长之和",
        frames == expected_frames and report["total_output_frames"] == expected_frames,
        "实际 %d 帧 / 期望 %d 帧" % (frames, expected_frames),
    )
    check(
        "1. 三个片段均完整且按配置顺序排列",
        [e["index"] for e in report["complete_segments"]] == [0, 1, 2]
        and report["counts"] == {"complete": 3, "partial": 0, "failed": 0, "total": 3},
    )
    # 额外确定性校验：输出字节流 == 三个源片段字节切片的顺序拼接
    with wave.open(proc.out_wav, "rb") as wf:
        out_bytes = wf.readframes(frames)
    expected_bytes = b""
    for src, s, e in ((a, 0.0, 2.0), (b, 0.5, 2.0), (c, 1.0, 4.0)):
        with wave.open(src, "rb") as wf:
            sf, ef = int(round(s * wf.getframerate())), int(round(e * wf.getframerate()))
            wf.setpos(sf)
            expected_bytes += wf.readframes(ef - sf)
    check("1. 输出 PCM 字节与源片段顺序拼接完全一致", out_bytes == expected_bytes)

    # ---- 验收 2：参数不一致片段跳过，输出参数同首个可用片段 ----------- #
    cfg2 = {
        "clips": [
            {"source": a, "start": 0.0, "end": 1.0},      # 8000Hz 单声道
            {"source": diff, "start": 0.0, "end": 1.0},   # 16000Hz 立体声 -> 失败
            {"source": b, "start": 1.0, "end": 2.0},
        ]
    }
    proc = run_editor("t2_param_mismatch", cfg2)
    report = load_report(proc)
    ch2, rate2, width2, frames2 = wav_info(proc.out_wav)
    check(
        "2. 输出参数与首个可用片段一致（8000Hz/单声道/2字节）",
        (ch2, rate2, width2) == (1, 8000, 2),
        "实际 %d 声道 %d Hz %d 字节" % (ch2, rate2, width2),
    )
    failed_idx = [e["index"] for e in report["failed_segments"]]
    check(
        "2. 参数不一致片段被跳过并记录失败原因",
        failed_idx == [1] and "参数" in report["failed_segments"][0]["reason"],
        report["failed_segments"][0]["reason"] if failed_idx else "无失败条目",
    )
    check("2. 输出只含两个一致片段（2.0 秒）", frames2 == 2 * 8000)
    check("2. 报告状态为 partial", report["status"] == "partial")

    # ---- 验收 3：源文件缺失，其余片段正常拼接 ------------------------- #
    cfg3 = {
        "clips": [
            {"source": miss_a, "start": 0.0, "end": 1.0},
            {"source": miss_b, "start": 0.0, "end": 1.0},  # 已删除
            {"source": miss_c, "start": 0.0, "end": 1.0},
        ]
    }
    proc = run_editor("t3_missing", cfg3)
    report = load_report(proc)
    _, _, _, frames3 = wav_info(proc.out_wav)
    check(
        "3. 缺失片段失败且原因包含‘缺失’",
        [e["index"] for e in report["failed_segments"]] == [1]
        and "缺失" in report["failed_segments"][0]["reason"],
    )
    check("3. 其余两个片段正常拼接（2.0 秒）", frames3 == 2 * 8000)

    # ---- 验收 4：结束秒越界 -> 截取到源末尾，标记部分完成 ------------- #
    cfg4 = {
        "clips": [
            {"source": a, "start": 3.0, "end": 9.0},      # 源 5s，剩 2s
            {"source": b, "start": 0.0, "end": 1.0},
        ]
    }
    proc = run_editor("t4_overlong", cfg4)
    report = load_report(proc)
    _, _, _, frames4 = wav_info(proc.out_wav)
    partial = report["partial_segments"]
    check(
        "4. 越界片段标记 partial 且给出原因",
        [e["index"] for e in partial] == [0]
        and partial[0]["output_frames"] == 2 * 8000
        and "源文件末尾" in partial[0]["reason"],
        partial[0]["reason"] if partial else "无部分完成条目",
    )
    check("4. 输出时长 = 剩余 2s + 完整 1s = 3s", frames4 == 3 * 8000)

    # ---- 验收 5：报告三列表按配置顺序、条目含索引与非空原因 ----------- #
    cfg5 = {
        "clips": [
            {"source": a, "start": 0.0, "end": 1.0},       # complete idx0
            {"source": diff, "start": 0.0, "end": 1.0},    # failed   idx1
            {"source": b, "start": 2.0, "end": 8.0},       # partial  idx2（b 仅 4s）
        ]
    }
    proc = run_editor("t5_report", cfg5)
    report = load_report(proc)
    ordering_ok = (
        [e["index"] for e in report["complete_segments"]] == [0]
        and [e["index"] for e in report["failed_segments"]] == [1]
        and [e["index"] for e in report["partial_segments"]] == [2]
    )
    check("5. 三个类别列表均存在且索引顺序与配置一致", ordering_ok)
    check("5. 每个条目均含片段索引且原因字符串非空", assert_reasons_nonempty(report))

    # 对所有已生成报告统一复核原因非空
    all_reports_ok = True
    for root_dir, _dirs, files in os.walk(OUT):
        for name in files:
            if name == "report.json":
                with open(os.path.join(root_dir, name), "r", encoding="utf-8") as fh:
                    rep = json.load(fh)
                all_reports_ok &= assert_reasons_nonempty(rep)
    check("5. 全部场景报告的所有条目原因均非空", all_reports_ok)

    # ---- 验收 6：全部片段失败 -> 无 WAV、状态 failed、退出码非零 ------ #
    cfg6 = {
        "clips": [
            {"source": corrupt, "start": 0.0, "end": 1.0},
            {"source": ghost, "start": 0.0, "end": 1.0},
        ]
    }
    proc = run_editor("t6_all_failed", cfg6)
    report = load_report(proc)
    out_dir = os.path.dirname(proc.out_wav)
    wav_files = [n for n in os.listdir(out_dir) if n.lower().endswith(".wav")]
    check("6. 全部失败时退出码非零", proc.returncode != 0, "退出码 %d" % proc.returncode)
    check("6. 输出目录不存在任何 WAV 文件", wav_files == [], str(wav_files))
    check(
        "6. 报告状态为 failed 且两个片段均在失败列表",
        report["status"] == "failed"
        and [e["index"] for e in report["failed_segments"]] == [0, 1],
    )

    # ---- 验收 7：配置非法 -> 错误提示 + 非零退出码 -------------------- #
    bad_configs = {
        "empty_clips": {"clips": []},
        "missing_field": {"clips": [{"source": a, "start": 0.0}]},
        "missing_clips": {"segments": []},
    }
    for case, cfg in bad_configs.items():
        proc = run_editor("t7_" + case, cfg)
        stderr_text = proc.stderr.decode("utf-8", "replace")
        check(
            "7. 非法配置[%s]退出码非零且控制台有错误提示" % case,
            proc.returncode != 0 and "错误" in stderr_text and len(stderr_text.strip()) > 0,
            stderr_text.strip().splitlines()[0] if stderr_text.strip() else "无输出",
        )

    # ---- 验收 8：wave 模块可读，文件头与首个可用片段一致 -------------- #
    proc = run_editor("t8_header", cfg1)
    with wave.open(proc.out_wav, "rb") as wf_out:
        hdr = (wf_out.getnchannels(), wf_out.getframerate(), wf_out.getsampwidth())
    with wave.open(a, "rb") as wf_a:
        ref_hdr = (wf_a.getnchannels(), wf_a.getframerate(), wf_a.getsampwidth())
    check(
        "8. 标准 wave 模块可读取且文件头与首个成功片段一致",
        hdr == ref_hdr == (1, 8000, 2),
        "输出头=%s 参考头=%s" % (hdr, ref_hdr),
    )

    # ---- 汇总 ---------------------------------------------------------- #
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n========== 验收汇总: %d/%d 通过 ==========" % (passed, total))
    if passed != total:
        for name, ok, detail in results:
            if not ok:
                print("  未通过: %s %s" % (name, detail))
        return 1
    print("全部验收通过。工作区: %s" % WORK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
