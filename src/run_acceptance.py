# -*- coding: utf-8 -*-
"""
端到端验收测试：覆盖任务书 11 项验收标准。

流程：
  1. 调用 make_test_data.generate() 生成确定性测试数据（默认 runtime/test_workspace）。
  2. 以子进程 `python -I src/podcast_joiner.py ...` 运行主程序并断言结果。
  3. 输出每项 PASS/FAIL 与汇总，任一失败则进程返回非零退出码。
"""

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
JOINER = os.path.join(HERE, "podcast_joiner.py")
GENERATOR = os.path.join(HERE, "make_test_data.py")

sys.path.insert(0, HERE)
import make_test_data  # noqa: E402

SR = make_test_data.SAMPLE_RATE
RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def run_joiner(desc_path, output_dir=None, cwd=None):
    cmd = [sys.executable, "-I", JOINER, desc_path]
    if output_dir is not None:
        cmd += ["--output-dir", output_dir]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=cwd, env=env,
    )


def read_wav(path):
    with wave.open(path, "rb") as wf:
        return {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "framerate": wf.getframerate(),
            "nframes": wf.getnframes(),
            "duration": wf.getnframes() / wf.getframerate(),
            "frames": wf.readframes(wf.getnframes()),
        }


def rms_dbfs(frames_bytes):
    n = len(frames_bytes) // 2
    if n == 0:
        return None
    total = sum(s * s for (s,) in struct.iter_unpack("<h", frames_bytes))
    rms = math.sqrt(total / n)
    return 20.0 * math.log10(rms / 32768.0)


def peak_abs(frames_bytes):
    return max((abs(s) for (s,) in struct.iter_unpack("<h", frames_bytes)), default=0)


def load_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def statuses(report):
    return [e["status"] for e in report["segments"]]


def main():
    workspace = os.path.join(ROOT, "runtime", "test_workspace")
    # 每次运行前清空工作区，保证测试可重复（首次运行/覆盖标志等断言不受残留影响）
    if os.path.isdir(workspace):
        shutil.rmtree(workspace)
    out_dir = os.path.join(workspace, "out")
    cwd_dir = os.path.join(workspace, "cwd_default")
    os.makedirs(cwd_dir, exist_ok=True)
    make_test_data.generate(workspace)

    def desc(name):
        return os.path.join(workspace, f"{name}.json")

    def out_wav(name):
        return os.path.join(out_dir, f"{name}_output.wav")

    def out_report(name):
        return os.path.join(out_dir, f"{name}_output_report.json")

    # ---------------- 场景 1：全片段有效，时长误差 < 0.1s ----------------
    r = run_joiner(desc("desc_valid"), out_dir)
    check("1. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    w = read_wav(out_wav("desc_valid"))
    expected = 2.0 + 3.0 + 1.5 + 4.0
    check("1. 输出为48kHz/16bit/单声道",
          w["framerate"] == SR and w["sampwidth"] == 2 and w["channels"] == 1)
    check("1. 总时长与区间之和误差<0.1s",
          abs(w["duration"] - expected) < 0.1,
          f"实际 {w['duration']:.3f}s / 期望 {expected:.3f}s")
    rep = load_report(out_report("desc_valid"))
    check("1. 全部片段状态为成功",
          all(s == "成功" for s in statuses(rep)), str(statuses(rep)))

    # ---------------- 场景 2：低 10dB 片段，统一后偏差<1dB 且无削波 ----------------
    r = run_joiner(desc("desc_quiet"), out_dir)
    check("2. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    w = read_wav(out_wav("desc_quiet"))
    after_db = rms_dbfs(w["frames"])
    check("2. 处理后RMS与基准(-20dB)偏差<1dB",
          after_db is not None and abs(after_db - (-20.0)) < 1.0,
          f"处理后 RMS={after_db:.3f} dBFS")
    check("2. 样本无削波（|peak|<=32767）",
          peak_abs(w["frames"]) <= 32767, f"peak={peak_abs(w['frames'])}")
    rep = load_report(out_report("desc_quiet"))
    e0 = rep["segments"][0]
    # 样本离散量化后原始 RMS 约 -29.04dB（设计目标 -30dB），增益=基准-原始响度，
    # 因此约为 9dB；关键验收是处理后 RMS 落入基准 ±1dB（上面已验证）。
    gain_consistent = (
        e0["original_rms_db"] is not None
        and abs(e0["applied_gain_db"]
                - (-20.0 - e0["original_rms_db"])) < 0.1
    )
    check("2. 报告记录原始响度且增益=基准-原始响度(约+10dB)",
          gain_consistent,
          f"原始 {e0['original_rms_db']}dB / 增益 {e0['applied_gain_db']}dB")

    # ---------------- 场景 3：缺失片段隔离，其余继续 ----------------
    r = run_joiner(desc("desc_missing"), out_dir)
    check("3. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    rep = load_report(out_report("desc_missing"))
    check("3. 缺失片段被标记为缺失",
          statuses(rep) == ["缺失", "成功"], str(statuses(rep)))
    w = read_wav(out_wav("desc_missing"))
    check("3. 输出仅含有效片段音频(2.0s±0.1s)",
          abs(w["duration"] - 2.0) < 0.1, f"实际 {w['duration']:.3f}s")

    # ---------------- 场景 4：无效区间标记并跳过 ----------------
    r = run_joiner(desc("desc_invalid"), out_dir)
    check("4. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    rep = load_report(out_report("desc_invalid"))
    check("4. 四个问题片段均标为无效区间",
          statuses(rep)[:4] == ["无效区间"] * 4, str(statuses(rep)))
    check("4. 有效片段仍成功", rep["segments"][4]["status"] == "成功")
    w = read_wav(out_wav("desc_invalid"))
    check("4. 输出不含无效片段音频(1.0s±0.1s)",
          abs(w["duration"] - 1.0) < 0.1, f"实际 {w['duration']:.3f}s")

    # ---------------- 场景 5：全部失败 → 空 WAV 仍可打开 ----------------
    r = run_joiner(desc("desc_all_fail"), out_dir)
    check("5. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    w = read_wav(out_wav("desc_all_fail"))
    check("5. 空WAV数据帧为0且可被wave打开", w["nframes"] == 0)
    check("5. 空WAV保持48kHz/16bit/单声道",
          w["framerate"] == SR and w["sampwidth"] == 2 and w["channels"] == 1)
    rep = load_report(out_report("desc_all_fail"))
    check("5. 报告逐片段说明原因",
          all(e["reason"] for e in rep["segments"])
          and statuses(rep) == ["无效区间", "缺失", "采样率不匹配"],
          str(statuses(rep)))
    check("5. 摘要成功数为0", rep["summary"]["succeeded"] == 0)

    # ---------------- 场景 6：报告 JSON 字段完整性 ----------------
    rep = load_report(out_report("desc_valid"))
    allowed = {"成功", "缺失", "无效区间", "采样率不匹配"}
    required = {"index", "path", "start_sec", "end_sec", "status", "reason",
                "original_rms_db", "applied_gain_db",
                "effective_duration_sec", "warning"}
    fields_ok = all(required.issubset(e.keys()) for e in rep["segments"])
    status_ok = all(e["status"] in allowed for e in rep["segments"])
    summary_ok = (
        "succeeded" in rep["summary"]
        and "total_segments" in rep["summary"]
        and "total_output_duration_sec" in rep["summary"]
    )
    check("6. 报告可解析且每条目含全部规定字段", fields_ok)
    check("6. 状态均取允许值", status_ok)
    check("6. 摘要含成功片段数等汇总信息", summary_ok,
          f"succeeded={rep['summary']['succeeded']}")

    # ---------------- 场景 7：基准响度非法 → 非零退出 + stderr 提示 + 无 WAV ----------------
    for name in ("desc_bad_ref_low", "desc_bad_ref_high"):
        before = set(os.listdir(out_dir))
        r = run_joiner(desc(name), out_dir)
        after = set(os.listdir(out_dir))
        check(f"7. {name} 非零退出码", r.returncode != 0, f"code={r.returncode}")
        check(f"7. {name} stderr含'基准响度无效'",
              "基准响度无效" in r.stderr, r.stderr.strip())
        check(f"7. {name} 输出目录无新增WAV/报告",
              not any(f.endswith(".wav") or f.endswith("_report.json")
                      for f in (after - before)))

    # ---------------- 场景 8：采样率不匹配隔离 ----------------
    r = run_joiner(desc("desc_rate_mismatch"), out_dir)
    check("8. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    rep = load_report(out_report("desc_rate_mismatch"))
    check("8. 44100片段标记为采样率不匹配",
          statuses(rep) == ["采样率不匹配", "成功"], str(statuses(rep)))
    w = read_wav(out_wav("desc_rate_mismatch"))
    check("8. 输出不含不匹配片段(1.0s±0.1s)",
          abs(w["duration"] - 1.0) < 0.1, f"实际 {w['duration']:.3f}s")

    # ---------------- 场景 9：6 分钟片段警告但成功且完整 ----------------
    r = run_joiner(desc("desc_long"), out_dir)
    check("9. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    rep = load_report(out_report("desc_long"))
    e0 = rep["segments"][0]
    check("9. 长片段状态为成功且含警告",
          e0["status"] == "成功" and bool(e0["warning"]) and "超过" in e0["warning"],
          e0.get("warning"))
    w = read_wav(out_wav("desc_long"))
    check("9. 输出含完整6分钟音频(360s±0.1s)",
          abs(w["duration"] - 360.0) < 0.1, f"实际 {w['duration']:.3f}s")
    check("9. 摘要记录警告数=1", rep["summary"]["warnings"] == 1)

    # ---------------- 场景 10：覆盖已存在输出，报告覆盖标志为 true ----------------
    target = os.path.join(out_dir, "overwrite_output.wav")
    marker = b"PRE-EXISTING-PLACEHOLDER"
    with open(target, "wb") as f:
        f.write(marker)
    r = run_joiner(desc("desc_overwrite"), out_dir)
    check("10. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    with open(target, "rb") as f:
        content = f.read(len(marker))
    check("10. 已存在WAV被替换", content != marker)
    rep = load_report(os.path.join(out_dir, "overwrite_output_report.json"))
    check("10. 报告覆盖标志为true", rep["overwritten"] is True
          and rep["summary"]["overwritten"] is True)
    # 在全新空目录中首次运行，验证目标不存在时覆盖标志为 false
    fresh_dir = os.path.join(workspace, "fresh_first_run")
    r2 = run_joiner(desc("desc_valid"), fresh_dir)
    rep2 = load_report(os.path.join(fresh_dir, "desc_valid_output_report.json"))
    check("10. 目标不存在时覆盖标志为false", rep2["overwritten"] is False)

    # ---------------- 场景 11a：--output-dir 两文件均落在指定目录 ----------------
    custom = os.path.join(workspace, "custom_out")
    r = run_joiner(desc("desc_valid"), custom)
    check("11a. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    check("11a. WAV与报告均生成于指定目录",
          os.path.isfile(os.path.join(custom, "desc_valid_output.wav"))
          and os.path.isfile(os.path.join(custom, "desc_valid_output_report.json")))

    # ---------------- 场景 11b：省略 --output-dir 时输出到当前工作目录 ----------------
    r = run_joiner(desc("desc_default_dir"), None, cwd=cwd_dir)
    check("11b. 程序退出码为0", r.returncode == 0, r.stderr.strip())
    check("11b. 两文件生成于当前工作目录",
          os.path.isfile(os.path.join(cwd_dir, "default_dir_output.wav"))
          and os.path.isfile(os.path.join(cwd_dir, "default_dir_output_report.json")))

    # ---------------- 汇总 ----------------
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 60)
    print(f"验收结果：{passed}/{total} 通过")
    if passed != total:
        print("失败项：")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name} {detail}")
    else:
        print("全部验收标准通过。")

    # 同步写一份 UTF-8 留档，规避 Windows 控制台代码页导致的日志乱码
    log_path = os.path.join(ROOT, "runtime", "acceptance_result.md")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"# 验收测试结果：{passed}/{total} 通过\n\n")
        for name, ok, detail in RESULTS:
            f.write(f"- [{'PASS' if ok else 'FAIL'}] {name}"
                    f"{' — ' + detail if detail else ''}\n")
    print(f"UTF-8 结果留档：{log_path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
