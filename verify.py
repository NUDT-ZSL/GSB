# -*- coding: utf-8 -*-
"""
端到端验证脚本：确定性生成样本 -> 运行 process.py -> 逐条核对 8 个验收点。

仅使用标准库。任一验收点失败则以退出码 1 结束，全部通过退出码 0。

用法:
  python verify.py
"""

import json
import math
import os
import re
import subprocess
import sys
import wave

# Windows 控制台可能为 GBK，统一标准输出编码避免中文崩溃
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(ROOT, "work")
SAMPLES = os.path.join(WORK, "samples")
OUT = os.path.join(WORK, "output")
PY = sys.executable

PASS, FAIL = "PASS", "FAIL"
results = []


def check(point, condition, detail=""):
    results.append((point, bool(condition), detail))
    flag = PASS if condition else FAIL
    print("[%s] %s %s" % (flag, point, ("- " + detail) if detail else ""))


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def wav_info(path):
    with wave.open(path, "rb") as wf:
        return {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "rate": wf.getframerate(),
            "frames": wf.getnframes(),
            "duration": wf.getnframes() / wf.getframerate(),
        }


def region_rms_dbfs(path, start_frame, end_frame, rate):
    import array
    with wave.open(path, "rb") as wf:
        wf.setpos(start_frame)
        raw = wf.readframes(end_frame - start_frame)
    samples = array.array("h")
    samples.frombytes(raw)
    if not samples:
        return float("-inf"), 0
    total = sum(s * s for s in samples)
    rms = math.sqrt(total / len(samples))
    db = float("-inf") if rms <= 0 else 20 * math.log10(rms / 32768.0)
    return db, len(samples)


# --------------------------------------------------------------------------- #
# 0. 准备：生成确定性样本
# --------------------------------------------------------------------------- #
print("== 准备: 生成确定性样本 ==")
r = run([PY, "-I", "make_samples.py", "--workdir", "work"])
if r.returncode != 0:
    print(r.stdout)
    print(r.stderr)
    sys.exit(1)
print(r.stdout.strip())
print()

# --------------------------------------------------------------------------- #
# 验收点 8：无参数运行，退出码非 0 且 stdout 含用法
# --------------------------------------------------------------------------- #
print("== 验收点 8: 命令行缺参处理 ==")
r = run([PY, "-I", "process.py"])
check("8a 无参数退出码非0", r.returncode != 0, "returncode=%d" % r.returncode)
check("8b stdout 含用法说明", "usage" in r.stdout.lower() and "--manifest" in r.stdout,
      "stdout 前80字符: %r" % r.stdout[:80])
print()

# --------------------------------------------------------------------------- #
# 主清单运行
# --------------------------------------------------------------------------- #
print("== 运行主清单 edit_list.json ==")
main_out = os.path.join(OUT, "dialogue_main.wav")
main_report = os.path.join(OUT, "report_main.json")
main_log = os.path.join(OUT, "run_main.log")
r = run([PY, "-I", "process.py",
         "--manifest", os.path.join(SAMPLES, "edit_list.json"),
         "--output", main_out,
         "--report", main_report,
         "--log", main_log])
check("主清单运行退出码0", r.returncode == 0, r.stderr.strip())
print()

with open(main_report, "r", encoding="utf-8") as f:
    report = json.load(f)
items = report["segments"]
by_file = {}
for it in items:
    by_file.setdefault(it["file"], []).append(it)

with open(main_log, "r", encoding="utf-8") as f:
    log_text = f.read()
log_lines = [ln for ln in log_text.splitlines() if ln.strip()]

# --------------------------------------------------------------------------- #
# 验收点 1：缺失文件与非 PCM 条目给错误原因，其余继续处理
# --------------------------------------------------------------------------- #
print("== 验收点 1: 缺失/非PCM/格式不符标注错误，其余继续 ==")
check("1a 缺失文件状态=错误且有原因",
      by_file["missing.wav"][0]["status"] == "错误"
      and "不存在" in by_file["missing.wav"][0]["reason"],
      str(by_file["missing.wav"][0]))
check("1b IEEE float 状态=错误且有原因",
      by_file["float_2s.wav"][0]["status"] == "错误"
      and by_file["float_2s.wav"][0]["reason"] is not None,
      str(by_file["float_2s.wav"][0]))
check("1c 8位片段状态=错误",
      by_file["pcm8_2s.wav"][0]["status"] == "错误"
      and "16位" in by_file["pcm8_2s.wav"][0]["reason"],
      str(by_file["pcm8_2s.wav"][0]))
check("1d 立体声片段状态=错误",
      by_file["stereo_2s.wav"][0]["status"] == "错误"
      and "单声道" in by_file["stereo_2s.wav"][0]["reason"],
      str(by_file["stereo_2s.wav"][0]))
check("1e 错误条目之后的合法片段仍成功",
      by_file["good_1s_44100.wav"][-1]["status"] == "成功",
      str(by_file["good_1s_44100.wav"][-1]))
print()

# --------------------------------------------------------------------------- #
# 验收点 2：越界截断 0.5s；起始大于结束 -> 跳过
# --------------------------------------------------------------------------- #
print("== 验收点 2: 裁剪越界截断与非法区间跳过 ==")
clamp_item = by_file["good_2s_22050.wav"][1]
check("2a 2s素材请求1.5-3s输出0.5s(±1帧)",
      clamp_item["status"] == "成功"
      and abs(clamp_item["duration_seconds"] - 0.5) <= 1 / 44100 + 1e-9,
      "duration=%s" % clamp_item["duration_seconds"])
skip_item = by_file["good_1s_44100.wav"][0]
check("2b 起始大于结束状态=跳过且时长null",
      skip_item["status"] == "跳过" and skip_item["duration_seconds"] is None
      and skip_item["reason"] is not None, str(skip_item))
print()

# --------------------------------------------------------------------------- #
# 验收点 3：统一目标采样率，WAV 头与帧数换算
# --------------------------------------------------------------------------- #
print("== 验收点 3: 重采样与 WAV 头 ==")
info = wav_info(main_out)
check("3a 输出头采样率=44100", info["rate"] == 44100, str(info["rate"]))
check("3b 输出头声道数=1 采样宽度=2字节",
      info["channels"] == 1 and info["sampwidth"] == 2,
      "channels=%d width=%d" % (info["channels"], info["sampwidth"]))
# 22050Hz 2.0s -> 44100Hz 应为 88200 帧（±1）
first = by_file["good_2s_22050.wav"][0]
check("3c 22050Hz片段重采样帧数按44100换算(±1)",
      abs(first["duration_seconds"] - 2.0) <= 1 / 44100 + 1e-9,
      "duration=%s" % first["duration_seconds"])
print()

# --------------------------------------------------------------------------- #
# 验收点 4：各成功片段 RMS 与目标 dBFS 误差 ≤ ±0.5dB
# 按报告条目与清单条目顺序对齐，重建每个成功片段在拼接流中的帧偏移
# --------------------------------------------------------------------------- #
print("== 验收点 4: 响度归一化精度 ==")
manifest = json.load(open(os.path.join(SAMPLES, "edit_list.json"), encoding="utf-8"))
pairs = list(zip(items, manifest["segments"]))
cursor = 0
loud_ok = True
loud_detail = []
for idx, (it, seg) in enumerate(pairs):
    if it["status"] != "成功":
        continue
    n_frames = int(round(it["duration_seconds"] * 44100))
    db, n = region_rms_dbfs(main_out, cursor, cursor + n_frames, 44100)
    err = abs(db - seg["target_dbfs"])
    loud_detail.append("%s 目标%.1f 实测%.2f 误差%.2fdB"
                       % (it["file"], seg["target_dbfs"], db, err))
    if err > 0.5:
        loud_ok = False
    cursor += n_frames
    # 后续仍有成功片段时，插入该条目声明的片段间静音
    later_success = any(x["status"] == "成功" for x in items[idx + 1:])
    if later_success:
        cursor += int(round(seg["silence_after_ms"] * 44100 / 1000.0))
check("4 全部成功片段RMS误差≤0.5dB", loud_ok, "; ".join(loud_detail))
print()

# --------------------------------------------------------------------------- #
# 验收点 5：间隔清单，两个0.5s片段+200ms，成功片段总长1.2s（±1采样）
# --------------------------------------------------------------------------- #
print("== 验收点 5: 拼接与片段间静音 ==")
gap_out = os.path.join(OUT, "dialogue_gap.wav")
gap_report = os.path.join(OUT, "report_gap.json")
gap_log = os.path.join(OUT, "run_gap.log")
r = run([PY, "-I", "process.py",
         "--manifest", os.path.join(SAMPLES, "gap_list.json"),
         "--output", gap_out, "--report", gap_report, "--log", gap_log])
check("5a 间隔清单运行退出码0", r.returncode == 0, r.stderr.strip())
ginfo = wav_info(gap_out)
expected_frames = int(round(0.5 * 44100)) * 2 + int(round(0.2 * 44100))
check("5b 总帧数=两片段+200ms静音(±1采样)",
      abs(ginfo["frames"] - expected_frames) <= 1,
      "frames=%d expected=%d" % (ginfo["frames"], expected_frames))
gap_report_data = json.load(open(gap_report, encoding="utf-8"))
audio_only = sum(it["duration_seconds"] for it in gap_report_data["segments"]
                 if it["status"] == "成功")
check("5c 输出WAV总时长1.2s(含200ms静音, ±1采样)",
      abs(ginfo["duration"] - 1.2) <= 1 / 44100 + 1e-9,
      "wav时长=%s" % ginfo["duration"])
check("5c-补 两片段纯音频各0.5s合计1.0s",
      abs(audio_only - 1.0) <= 1 / 44100 + 1e-9, "纯音频合计=%s" % audio_only)
# 中间静音区确实为 0
import array
with wave.open(gap_out, "rb") as wf:
    half = int(round(0.5 * 44100))
    gap_n = int(round(0.2 * 44100))
    wf.setpos(half)
    gap_raw = wf.readframes(gap_n)
gap_samples = array.array("h")
gap_samples.frombytes(gap_raw)
check("5d 200ms间隔区采样全为0", all(s == 0 for s in gap_samples),
      "非零采样数=%d" % sum(1 for s in gap_samples if s != 0))
# 错误/跳过片段不留空洞已由主清单验证（总帧数只含成功片段+成功片段间静音）
main_expected = 0
succ = [it for it in items if it["status"] == "成功"]
main_expected += sum(int(round(it["duration_seconds"] * 44100)) for it in succ)
for idx, it in enumerate(succ):
    if idx < len(succ) - 1:
        main_expected += int(round(100 * 44100 / 1000.0))
check("5e 主输出不含错误/跳过片段的时间空洞",
      abs(info["frames"] - main_expected) <= 1,
      "frames=%d expected=%d" % (info["frames"], main_expected))
print()

# --------------------------------------------------------------------------- #
# 验收点 6：报告字段齐全、状态枚举正确、总时长与 WAV 实测一致
# --------------------------------------------------------------------------- #
print("== 验收点 6: JSON 报告 ==")
fields_ok = all(set(it.keys()) >= {"file", "status", "reason", "duration_seconds"}
                for it in items)
enum_ok = all(it["status"] in ("成功", "跳过", "错误") for it in items)
null_reason_ok = all(it["reason"] is None for it in items if it["status"] == "成功")
check("6a 每条含 file/status/reason/duration_seconds", fields_ok)
check("6b 状态枚举仅为 成功/跳过/错误", enum_ok)
check("6c 成功条目 reason=null", null_reason_ok)
check("6d 报告总时长与WAV实测一致(±1采样)",
      abs(report["total_duration_seconds"] - info["duration"]) <= 1 / 44100 + 1e-9,
      "report=%s wav=%s" % (report["total_duration_seconds"], info["duration"]))
check("6e 报告总帧数与WAV一致", report["total_frames"] == info["frames"],
      "%d vs %d" % (report["total_frames"], info["frames"]))
check("6f summary 计数正确",
      report["summary"] == {"total": 8, "success": 3, "skipped": 1, "error": 4},
      str(report["summary"]))
print()

# --------------------------------------------------------------------------- #
# 验收点 7：日志格式与 ERROR 行
# --------------------------------------------------------------------------- #
print("== 验收点 7: 运行日志 ==")
line_re = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} (INFO|ERROR) .+")
fmt_ok = all(line_re.match(ln) for ln in log_lines)
check("7a 每行均为 时间戳 级别(INFO/ERROR) 消息", fmt_ok,
      "样例: %r" % (log_lines[0] if log_lines else ""))
err_lines = [ln for ln in log_lines if " ERROR " in ln]
missing_err = [ln for ln in err_lines if "missing.wav" in ln and "不存在" in ln]
check("7b 缺失文件存在对应ERROR行", len(missing_err) >= 1,
      missing_err[0] if missing_err else "未找到")
check("7c 4个错误片段各至少1条ERROR", len(err_lines) >= 4,
      "ERROR行数=%d" % len(err_lines))
print()

# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
total = len(results)
passed = sum(1 for _, ok, _ in results if ok)
print("=" * 60)
print("验证结果: %d/%d 通过" % (passed, total))
if passed != total:
    for point, ok, detail in results:
        if not ok:
            print("  失败项: %s %s" % (point, detail))
    sys.exit(1)
print("全部验收点通过。")
sys.exit(0)
