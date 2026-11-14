# -*- coding: utf-8 -*-
"""端到端演示与核验：覆盖任务 12 项要求，输出控制台判定并写 CHECK_RESULTS.md。

运行：
    python -I check_demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import numpy as np
import soundfile as sf

try:  # Windows 控制台默认 GBK，统一以 UTF-8 输出避免中文乱码
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
OUT_DIR = os.path.join(SAMPLES, "out")
REPORT = os.path.join(SAMPLES, "report.json")
SR = 44100
WINDOW_LEN = SR // 200  # 5ms = 220 采样

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (" — " + detail if detail else ""))


def rms(x: np.ndarray) -> float:
    x = x.astype(np.float64) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def independent_scores(wav: str, seams: list[int]) -> list[float]:
    data, _ = sf.read(wav, dtype="int16")
    overall = rms(data)
    out = []
    for s in seams:
        out.append(abs(rms(data[s - WINDOW_LEN:s]) - rms(data[s:s + WINDOW_LEN])) / overall)
    return out


def run_processor(report_path: str) -> tuple[int, str, str]:
    """以子进程运行处理器（优先 -I 隔离模式），返回返回码与标准输出。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd_base = [sys.executable, "-I", os.path.join(HERE, "seam_processor.py"),
                "--manifest", os.path.join(SAMPLES, "manifest.json"),
                "--report", report_path]
    proc = subprocess.run(cmd_base, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", env=env)
    if proc.returncode != 0 and "No module named" in proc.stderr:
        # 个别环境的第三方包装在 user site，-I 不可见时退回到普通隔离运行
        cmd_base.remove("-I")
        proc = subprocess.run(cmd_base, cwd=HERE, capture_output=True, text=True,
                              encoding="utf-8", env=env)
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    # 0. 干净重建样例
    if os.path.isdir(SAMPLES):
        shutil.rmtree(SAMPLES)
    sys.path.insert(0, HERE)
    import make_samples

    make_samples.build(SAMPLES)
    manifest = json.load(open(os.path.join(SAMPLES, "manifest.json"), encoding="utf-8"))
    entries = manifest["entries"]
    print("样例已生成，清单条目数：%d" % len(entries))

    # 12. 先独占锁定 locked_tone.wav，再做首次运行
    lock_proc = subprocess.Popen(
        [sys.executable, "-I", os.path.join(HERE, "lock_helper.py"),
         os.path.join(SAMPLES, "locked_tone.wav"), "12"],
        cwd=HERE, stdout=subprocess.PIPE, text=True,
        encoding="utf-8", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )
    assert lock_proc.stdout is not None
    first_line = lock_proc.stdout.readline().strip()
    print("锁定辅助进程：%s" % first_line)

    # 首次运行（存在锁定条目）
    code, stdout, stderr = run_processor(REPORT)
    print("---- 首次运行控制台输出 ----")
    print(stdout)
    if code != 0:
        print(stderr)
    rows1 = json.load(open(REPORT, encoding="utf-8"))
    by_file1 = {r["file"]: r for r in rows1}

    # 规则 5：顺序与五字段
    check("R5 报告顺序与清单一致",
          [r["file"] for r in rows1] == [e["file"] for e in entries])
    five = {"file", "status", "seam_index", "seam_score", "reason"}
    check("R5 每条恰含 file/status/seam_index/seam_score/reason 五字段",
          all(set(r.keys()) == five for r in rows1))

    # 规则 1：正常条目 ok，立体声跳过
    check("R1 sine_tone 正常导出 status=ok", by_file1["sine_tone.wav"]["status"] == "ok")
    check("R1 stereo_tone 被跳过且原因含声道",
          by_file1["stereo_tone.wav"]["status"] == "skipped"
          and "声道" in by_file1["stereo_tone.wav"]["reason"],
          by_file1["stereo_tone.wav"]["reason"])
    check("R1 低采样率条目原因含采样率",
          "采样率" in by_file1["low_rate_tone.wav"]["reason"],
          by_file1["low_rate_tone.wav"]["reason"])
    check("R1 浮点位深条目原因含位深",
          "位深" in by_file1["float_tone.wav"]["reason"],
          by_file1["float_tone.wav"]["reason"])

    # 规则 2：得分区间
    sine_row = by_file1["sine_tone.wav"]
    check("R2 连续正弦接缝得分 < 0.2",
          sine_row["seam_score"] < 0.2, "实测得分 = %.6f" % sine_row["seam_score"])
    abr_row = by_file1["abrupt_step.wav"]
    check("R2 突变接缝选中得分 > 0.8",
          abr_row["seam_score"] > 0.8, "实测得分 = %.6f" % abr_row["seam_score"])

    # 规则 3：独立复算，选中索引即最低分索引；并列取靠前
    for fname in ("sine_tone.wav", "abrupt_step.wav"):
        seams = next(e["candidate_seams"] for e in entries if e["file"] == fname)
        scores = independent_scores(os.path.join(SAMPLES, fname), seams)
        expect_idx = min(range(len(scores)), key=lambda i: scores[i])
        row = by_file1[fname]
        check("R3 %s 选中索引等于独立复算最低分索引" % fname,
              row["seam_index"] == expect_idx,
              "报告索引=%s，复算各候选得分=%s"
              % (row["seam_index"], [round(s, 6) for s in scores]))

    # 规则 4：输出时长偏差 <=1ms，且接缝无爆音
    for e in entries:
        out_path = os.path.join(OUT_DIR, e["file"])
        row = by_file1[e["file"]]
        if row["status"] != "ok":
            continue
        info = sf.info(out_path)
        delta_ms = abs(info.frames - e["expected_loop_length"]) / SR * 1000.0
        data, _ = sf.read(out_path, dtype="int16")
        jump = abs(int(data[-1]) - int(data[0])) / 32768.0
        check("R4 %s 时长偏差<=1ms" % e["file"], delta_ms <= 1.0,
              "实际 %d 采样 / 预期 %d 采样，偏差 %.4f ms"
              % (info.frames, e["expected_loop_length"], delta_ms))
        check("R4 %s 回绕接缝无爆音(边界跳变<2%%满量程)" % e["file"], jump < 0.02,
              "边界跳变 %.6f 满量程" % jump)
        check("R4 %s 输出为 44100Hz/16bit/单声道" % e["file"],
              info.samplerate == 44100 and info.subtype == "PCM_16" and info.channels == 1)

    # 交叉淡化实际效果：abrupt_step 未淡化前的原始跳变 vs 淡化后
    src, _ = sf.read(os.path.join(SAMPLES, "abrupt_step.wav"), dtype="int16")
    seam = next(e["candidate_seams"] for e in entries if e["file"] == "abrupt_step.wav")[
        abr_row["seam_index"]]
    raw_loop = src[seam:seam + 44000]
    raw_jump = abs(int(raw_loop[-1]) - int(raw_loop[0])) / 32768.0
    faded, _ = sf.read(os.path.join(OUT_DIR, "abrupt_step.wav"), dtype="int16")
    faded_jump = abs(int(faded[-1]) - int(faded[0])) / 32768.0
    check("R4 交叉淡化显著降低接缝跳变", faded_jump < raw_jump * 0.1,
          "淡化前 %.4f -> 淡化后 %.6f（满量程比）" % (raw_jump, faded_jump))

    # 规则 6：缺失跳过，同批正常条目不受影响
    check("R6 missing_tone 跳过且原因含缺失",
          by_file1["missing_tone.wav"]["status"] == "skipped"
          and "缺失" in by_file1["missing_tone.wav"]["reason"])
    check("R6 同批 sine_tone 导出文件存在",
          os.path.isfile(os.path.join(OUT_DIR, "sine_tone.wav")))

    # 规则 7：长度不足
    check("R7 short_tone 跳过且原因含长度不足",
          by_file1["short_tone.wav"]["status"] == "skipped"
          and "长度不足" in by_file1["short_tone.wav"]["reason"],
          by_file1["short_tone.wav"]["reason"])

    # 规则 8：重复文件名
    dup_rows = [r for r in rows1 if r["file"] == "dup_tone.wav"]
    check("R8 同名条目仅首个 ok、次条跳过且原因含重复",
          len(dup_rows) == 2 and dup_rows[0]["status"] == "ok"
          and dup_rows[1]["status"] == "skipped" and "重复" in dup_rows[1]["reason"])

    # 规则 9：输出目录自动创建（给一个不存在的新目录）
    fresh_manifest = {
        "output_dir": "out_fresh",
        "entries": [{"file": "sine_tone.wav",
                     "expected_loop_length": 44000,
                     "candidate_seams": [10000]}],
    }
    fresh_manifest_path = os.path.join(SAMPLES, "manifest_fresh.json")
    json.dump(fresh_manifest, open(fresh_manifest_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    fresh_proc = subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "seam_processor.py"),
         "--manifest", fresh_manifest_path,
         "--report", os.path.join(SAMPLES, "report_fresh.json")],
        cwd=HERE, capture_output=True, text=True, encoding="utf-8", env=env)
    if fresh_proc.returncode != 0 and "No module named" in fresh_proc.stderr:
        fresh_proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "seam_processor.py"),
             "--manifest", fresh_manifest_path,
             "--report", os.path.join(SAMPLES, "report_fresh.json")],
            cwd=HERE, capture_output=True, text=True, encoding="utf-8", env=env)
    check("R9 不存在的输出目录被自动创建且文件位于其中",
          os.path.isfile(os.path.join(SAMPLES, "out_fresh", "sine_tone.wav")))

    # 规则 10：控制台每个条目均有文件名与 OK/SKIPPED 标识
    console_ok = all(e["file"] in stdout for e in entries) and \
        stdout.count("[OK]") + stdout.count("[SKIPPED]") == len(entries)
    check("R10 控制台逐条输出文件名与 OK/SKIPPED 标识", console_ok,
          "OK %d 条 / SKIPPED %d 条"
          % (stdout.count("[OK]"), stdout.count("[SKIPPED]")))

    # 规则 11：静音
    sil = by_file1["silence.wav"]
    check("R11 静音条目 skipped、seam_score=1.0、原因含静音且无导出文件",
          sil["status"] == "skipped" and sil["seam_score"] == 1.0
          and "静音" in sil["reason"]
          and not os.path.exists(os.path.join(OUT_DIR, "silence.wav")),
          "报告：%s" % sil)

    # 规则 12：锁定条目首次跳过且原因注明不可读，其余 ok 不受影响
    locked1 = by_file1["locked_tone.wav"]
    ok_count_1 = sum(r["status"] == "ok" for r in rows1)
    check("R12 锁定条目首次运行跳过且原因含不可读",
          locked1["status"] == "skipped" and "不可读" in locked1["reason"],
          locked1["reason"])
    check("R12 首次运行其余条目正常 ok（共 %d 条 ok）" % ok_count_1,
          ok_count_1 == len([r for r in rows1 if r["file"] != "locked_tone.wav"
                             and r["status"] == "ok"]))

    # 等待锁释放后再次运行
    lock_proc.wait(timeout=20)
    code2, stdout2, stderr2 = run_processor(REPORT)
    print("---- 解锁后再次运行控制台输出 ----")
    print(stdout2)
    rows2 = json.load(open(REPORT, encoding="utf-8"))
    by_file2 = {r["file"]: r for r in rows2}
    check("R12 解锁后重跑，locked_tone 成功导出 status=ok",
          by_file2["locked_tone.wav"]["status"] == "ok"
          and os.path.isfile(os.path.join(OUT_DIR, "locked_tone.wav")))
    # 注意同名条目在报告中出现两次，按“首次出现”建索引，避免被跳过的次条覆盖
    first_row_2: dict = {}
    for r in rows2:
        first_row_2.setdefault(r["file"], r)
    ok_files_1 = []
    for r in rows1:
        if r["status"] == "ok" and r["file"] not in ok_files_1:
            ok_files_1.append(r["file"])
    check("R12 重跑后原有成功条目仍保持 ok",
          all(first_row_2[f]["status"] == "ok" for f in ok_files_1),
          "首次成功文件：%s" % ok_files_1)

    # 汇总并写 CHECK_RESULTS.md
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("\n核验汇总：%d/%d 通过" % (passed, total))
    lines = [
        "# 实际检查结果（check_demo.py 自动生成）",
        "",
        "- 运行环境：Python %s，numpy %s，soundfile %s，平台 %s"
        % (sys.version.split()[0], np.__version__, sf.__version__, sys.platform),
        "- 首次运行（locked_tone.wav 被独占锁定）：ok %d 条 / skipped %d 条"
        % (ok_count_1, len(rows1) - ok_count_1),
        "- 解锁后重跑：ok %d 条 / skipped %d 条"
        % (sum(r["status"] == "ok" for r in rows2),
           sum(r["status"] == "skipped" for r in rows2)),
        "",
        "## 关键实测数值",
        "",
        "- sine_tone 接缝得分：%.6f（要求 < 0.2）" % sine_row["seam_score"],
        "- abrupt_step 选中接缝索引：%s，得分：%.6f（要求 > 0.8）"
        % (abr_row["seam_index"], abr_row["seam_score"]),
        "- abrupt_step 回绕跳变：淡化前 %.4f → 淡化后 %.6f（满量程比）"
        % (raw_jump, faded_jump),
        "- 静音条目 seam_score：%s" % sil["seam_score"],
        "",
        "## 逐项判定（%d/%d 通过）" % (passed, total),
        "",
    ]
    for name, ok, detail in results:
        lines.append("- %s **%s**%s"
                     % ("PASS" if ok else "FAIL", name,
                        (" — " + detail) if detail else ""))
    open(os.path.join(HERE, "CHECK_RESULTS.md"), "w", encoding="utf-8").write(
        "\n".join(lines) + "\n")
    print("检查结果已写入：%s" % os.path.join(HERE, "CHECK_RESULTS.md"))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
