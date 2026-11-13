# -*- coding: utf-8 -*-
"""
verify.py — 针对 clip_tool.py 的端到端验收脚本（仅标准库）

覆盖任务书 10 条验收要求，自动生成样本、运行工具、独立用 wave 复核输出。
任一检查失败则以退出码 1 结束并打印 FAIL 明细。

用法:
  python verify.py
"""

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import wave

BASE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE, "input")
OUTPUT_DIR = os.path.join(BASE, "output")
CONFIG = os.path.join(BASE, "config.json")
REPORT = os.path.join(OUTPUT_DIR, "report.json")
LOG = os.path.join(OUTPUT_DIR, "clip_tool.log")

RATE = 48000
results = []  # (编号, 是否通过, 说明)


def _force_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def check(req_id, ok, detail):
    results.append((req_id, ok, detail))
    print("[%s] %s — %s" % ("PASS" if ok else "FAIL", req_id, detail))


def run(cmd):
    """以隔离模式(-I)运行子进程，避免工作目录中的同名模块干扰。
    强制 UTF-8 环境，避免中文 Windows 控制台默认 GBK 导致解码失败。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        cmd, cwd=BASE, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env
    )
    return proc.returncode, proc.stdout, proc.stderr


def file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def find_entry(entries, filename, clip_index):
    for e in entries:
        if e["filename"] == filename and e["clip_index"] == clip_index:
            return e
    return None


def read_wav(path):
    with wave.open(path, "rb") as wf:
        return {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "rate": wf.getframerate(),
            "nframes": wf.getnframes(),
            "raw": wf.readframes(wf.getnframes()),
        }


def main():
    _force_utf8_stdio()
    py = sys.executable

    # ---- 准备：确定性生成样本 ----
    rc, out, err = run([py, "-I", "make_samples.py", "input"])
    if rc != 0:
        print("样本生成失败:\n", err)
        return 1

    # ---- 需求8 前置：删除输出目录，验证自动创建 ----
    if os.path.isdir(OUTPUT_DIR):
        for name in os.listdir(OUTPUT_DIR):
            os.remove(os.path.join(OUTPUT_DIR, name))
        os.rmdir(OUTPUT_DIR)
    existed_before = os.path.isdir(OUTPUT_DIR)

    # ---- 第一次运行工具（需求10：命令行入口） ----
    rc, stdout, stderr = run([py, "-I", "clip_tool.py", "config.json"])
    if rc != 0:
        print("clip_tool 异常退出 rc=%d\nstderr=%s" % (rc, stderr))
        return 1

    with open(REPORT, "r", encoding="utf-8") as f:
        report = json.load(f)
    entries = report["entries"]
    summary = report["summary"]

    wav_files = sorted(
        n for n in os.listdir(OUTPUT_DIR) if n.lower().endswith(".wav")
    )
    success_entries = [e for e in entries if e["status"] == "success"]
    error_entries = [e for e in entries if e["status"] == "error"]

    # ================= 需求 1：配置读取 + 输出 WAV =================
    check("R1", (not existed_before) and os.path.isdir(OUTPUT_DIR)
          and len(wav_files) == len(success_entries) == 4,
          "输出目录自动重建，成功 4 条 -> 4 个 WAV：%s" % wav_files)

    # ================= 需求 2：格式校验，错误隔离 =================
    stereo = find_entry(entries, "stereo_16bit.wav", 1)
    mono8 = find_entry(entries, "mono_8bit.wav", 1)
    broken = find_entry(entries, "broken.wav", 1)
    ok_r2 = all(e is not None and e["status"] == "error"
                and bool(e["error"]) and bool(e["message"])
                for e in (stereo, mono8, broken))
    ok_r2 = ok_r2 and len(success_entries) == 4
    check("R2", ok_r2,
          "立体声/8位/损坏文件均记录 error 且 message 非空，4 个合法文件照常输出")

    # ================= 需求 3：截取时长 ±5ms =================
    clip_440_1 = find_entry(entries, "tone_440.wav", 1)  # 0.1->0.6 = 0.5s
    info = read_wav(os.path.join(OUTPUT_DIR, clip_440_1["output_file"]))
    duration = info["nframes"] / info["rate"]
    ok_r3 = (abs(duration - 0.5) <= 0.005
             and abs(info["nframes"] - 0.5 * RATE) <= 1)
    check("R3", ok_r3,
          "0.5s 片段实际时长 %.6fs（帧数 %d，偏差 %.3f ms）"
          % (duration, info["nframes"], abs(duration - 0.5) * 1000))

    # ================= 需求 4：增益 + 钳制，无回绕 =================
    clip_440_2 = find_entry(entries, "tone_440.wav", 2)  # gain 2.0
    info2 = read_wav(os.path.join(OUTPUT_DIR, clip_440_2["output_file"]))
    out_samples = struct.unpack("<%dh" % info2["nframes"], info2["raw"])
    src = read_wav(os.path.join(INPUT_DIR, "tone_440.wav"))
    src_samples = struct.unpack("<%dh" % src["nframes"], src["raw"])
    start_f = int(round(0.0 * RATE))
    end_f = int(round(0.5 * RATE))
    expected = [max(-32768, min(32767, int(round(s * 2.0))))
                for s in src_samples[start_f:end_f]]
    no_wrap = all(-32768 <= s <= 32767 for s in out_samples)
    saturated = any(s == 32767 for s in out_samples)
    matches_clamp = list(out_samples) == expected
    report_peak = clip_440_2["stats"]["peak"]
    peak_in_domain = -32768 <= report_peak <= 32767
    check("R4", no_wrap and saturated and matches_clamp and peak_in_domain,
          "2.0 倍增益：报告 peak=%d（样本域内=%s），出现 32767 钳制点=%s，"
          "全部样本在 [-32768,32767]，与独立钳制计算完全一致=%s"
          % (report_peak, peak_in_domain, saturated, matches_clamp))

    # ================= 需求 5：标准 PCM 可被 wave 重开 =================
    fmt_ok = True
    detail5 = []
    for e in success_entries:
        inf = read_wav(os.path.join(OUTPUT_DIR, e["output_file"]))
        cond = (inf["channels"] == 1 and inf["sampwidth"] == 2
                and inf["rate"] == RATE and inf["nframes"] == e["frame_count"]
                and len(inf["raw"]) == inf["nframes"] * 2)
        fmt_ok = fmt_ok and cond
        detail5.append("%s(ch=%d,width=%d,rate=%d,frames=%d)"
                       % (e["output_file"], inf["channels"],
                          inf["sampwidth"] * 8, inf["rate"], inf["nframes"]))
    # RIFF/WAVE 魔数与 fmt/data 块标识复核
    with open(os.path.join(OUTPUT_DIR, success_entries[0]["output_file"]), "rb") as f:
        head = f.read(12)
    chunks_ok = head[0:4] == b"RIFF" and head[8:12] == b"WAVE"
    check("R5", fmt_ok and chunks_ok,
          "全部输出含 RIFF/WAVE 结构且 wave 可重开；" + "；".join(detail5))

    # ================= 需求 6：峰值/RMS/过零率 vs 正弦理论值 =================
    stats = clip_440_1["stats"]
    theory_peak = 20000
    theory_rms = 20000 / math.sqrt(2)
    theory_zcr = 2 * 440 / RATE
    err_peak = abs(stats["peak"] - theory_peak) / theory_peak
    err_rms = abs(stats["rms"] - theory_rms) / theory_rms
    err_zcr = abs(stats["zero_crossing_rate"] - theory_zcr) / theory_zcr
    ok_r6 = err_peak < 0.05 and err_rms < 0.05 and err_zcr < 0.05
    check("R6", ok_r6,
          "440Hz 正弦：peak=%s(理论%d, 误差%.3f%%) rms=%s(理论%.2f, 误差%.3f%%) "
          "zcr=%.8f(理论%.8f, 误差%.3f%%)"
          % (stats["peak"], theory_peak, err_peak * 100,
             stats["rms"], theory_rms, err_rms * 100,
             stats["zero_crossing_rate"], theory_zcr, err_zcr * 100))

    # ================= 需求 7：排序 + 缺失/越界错误 =================
    names = [
        (e["output_file"] or
         "%s_clip%d.wav" % (os.path.splitext(e["filename"])[0], e["clip_index"]))
        for e in entries
    ]
    sorted_ok = names == sorted(names, key=lambda x: x.lower())
    missing = find_entry(entries, "missing_file.wav", 1)
    oob = find_entry(entries, "tone_440.wav", 3)          # 0.8->1.5 越界
    empty = find_entry(entries, "tone_1k.wav", 2)         # 0.5->0.5 空片段
    ok_r7 = (sorted_ok
             and missing["status"] == "error" and bool(missing["message"])
             and oob["status"] == "error" and bool(oob["message"])
             and empty["status"] == "error" and bool(empty["message"]))
    check("R7", ok_r7,
          "报告顺序=%s；缺失/越界/空片段均 status=error 且 message 非空" % names)

    # ================= 需求 8：重名不覆盖（第二次运行） =================
    before = {n: file_md5(os.path.join(OUTPUT_DIR, n)) for n in wav_files}
    rc2, stdout2, stderr2 = run([py, "-I", "clip_tool.py", "config.json"])
    wav_files_2 = sorted(
        n for n in os.listdir(OUTPUT_DIR) if n.lower().endswith(".wav")
    )
    old_unchanged = all(
        file_md5(os.path.join(OUTPUT_DIR, n)) == md5 for n, md5 in before.items()
    )
    new_files = [n for n in wav_files_2 if n not in before]
    suffix_ok = all(("_clip" in n) for n in new_files) and len(new_files) == 4
    check("R8", rc2 == 0 and old_unchanged and suffix_ok,
          "二次运行：旧 4 个文件 MD5 全部不变；新增 %s" % new_files)

    # 重新读取二次运行后的报告用于 R9/R10
    with open(REPORT, "r", encoding="utf-8") as f:
        report = json.load(f)
    entries = report["entries"]
    summary = report["summary"]

    # ================= 需求 9：日志文件 =================
    with open(LOG, "r", encoding="utf-8") as f:
        log_text = f.read()
    start_lines = log_text.count("开始处理:")
    result_lines = log_text.count("结果:")
    has_summary = ("汇总: 成功 %d 个，错误 %d 个"
                   % (summary["success"], summary["error"])) in log_text
    ok_r9 = (start_lines == 10 and result_lines == 10 and has_summary)
    check("R9", ok_r9,
          "日志含开始行 %d 条、结果行 %d 条，末尾汇总与报告一致(成功%d/错误%d)"
          % (start_lines, result_lines, summary["success"], summary["error"]))

    # ================= 需求 10：控制台汇总 =================
    expect_line = "success=%d error=%d total=%d" % (
        summary["success"], summary["error"], summary["total"])
    ok_r10 = rc == 0 and expect_line in stdout
    check("R10", ok_r10,
          "控制台输出含 `%s`，与报告统计一致" % expect_line)

    # ---------------- 总判定 ----------------
    failed = [r for r in results if not r[1]]
    print("\n================ 验收汇总 ================")
    print("通过 %d/%d 项" % (len(results) - len(failed), len(results)))
    if failed:
        print("失败项：", [r[0] for r in failed])
        return 1
    print("全部验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
