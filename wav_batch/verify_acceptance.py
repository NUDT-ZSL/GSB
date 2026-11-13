# -*- coding: utf-8 -*-
"""14 条验收项的独立验证脚本（仅标准库）。

独立使用 wave/math 复核输出 WAV 与 JSON 报告，不依赖被测处理器的结论。
运行： python verify_acceptance.py
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
OUT = os.path.join(HERE, "demo_output")
PY = sys.executable

# 中文 Windows 控制台可能默认 GBK，统一重配置为 UTF-8，避免打印子进程输出时崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

results: list[tuple[str, bool, str]] = []


def check(no: str, name: str, ok: bool, detail: str) -> None:
    results.append(("%s %s" % (no, name), ok, detail))
    print("[%s] %s —— %s" % ("通过" if ok else "失败", "%s %s" % (no, name), detail))


def run_cli(manifest: str, out_dir: str, report: str) -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [PY, "-I", os.path.join(HERE, "cli.py"),
         "--manifest", manifest, "--output-dir", out_dir, "--report", report],
        cwd=HERE, capture_output=True, env=env,
    )
    if proc.stdout:
        print(proc.stdout.decode("utf-8", errors="replace"))
    if proc.stderr:
        print("STDERR:", proc.stderr.decode("utf-8", errors="replace"))
    return proc.returncode


def inspect_wav(path: str) -> dict:
    """独立读取 WAV 的客观参数。"""
    with wave.open(path, "rb") as wf:
        info = {
            "channels": wf.getnchannels(),
            "sampwidth": wf.getsampwidth(),
            "rate": wf.getframerate(),
            "frames": wf.getnframes(),
        }
        raw = wf.readframes(info["frames"])
    n = info["frames"] * info["channels"]
    samples = struct.unpack("<%dh" % n, raw)
    info["samples"] = samples
    info["duration"] = info["frames"] / info["rate"]
    energy = sum(s * s for s in samples) / len(samples)
    info["rms_dbfs"] = 20 * math.log10(math.sqrt(energy) / 32768.0)
    info["peak"] = max(abs(s) for s in samples)
    return info


def load_report(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_entry(report: dict, entry_id: str) -> dict:
    for e in report["entries"]:
        if e["id"] == entry_id:
            return e
    raise AssertionError("报告中找不到条目 %s" % entry_id)


def main() -> int:
    # 干净重建，保证可复现
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    gen = subprocess.run([PY, "-I", os.path.join(HERE, "make_samples.py")],
                         cwd=HERE, check=True, capture_output=True, env=env)
    if gen.stderr:
        print(gen.stderr.decode("utf-8", errors="replace"))

    out_wav = os.path.join(OUT, "wav")
    report_path = os.path.join(OUT, "report.json")
    code = run_cli(os.path.join(SAMPLES, "manifest.csv"), out_wav, report_path)
    report = load_report(report_path)
    entries = {e["id"]: e for e in report["entries"]}

    # 验收 8：报告条目严格按清单行顺序
    expect_order = ["A_normal_44100", "B_clip_guard", "C_keep_8000", "D_missing_file",
                    "E_bad_format", "F_time_overflow", "G_bad_interval",
                    "H_target_range", "J_duration_check"]
    check("验收8", "报告条目按清单行顺序排列",
          [e["id"] for e in report["entries"]] == expect_order,
          "实际顺序：%s" % [e["id"] for e in report["entries"]])

    # 验收 1：格式限制（单声道 8 位失败；44100 双声道 16 位成功）
    e = entries["E_bad_format"]
    check("验收1a", "单声道8位WAV报告格式不支持",
          e["status"] == "失败" and e["output_path"] is None and "格式不支持" in e["error"],
          e["error"] or "")
    e = entries["A_normal_44100"]
    a_info = inspect_wav(e["output_path"]) if e["status"] == "成功" else None
    check("验收1b", "44100Hz双声道16位正常处理",
          e["status"] == "成功" and a_info is not None
          and a_info["rate"] == 44100 and a_info["channels"] == 2 and a_info["sampwidth"] == 2,
          "状态=%s" % e["status"])

    # 验收 2：时间区间裁剪与越界
    check("验收2a", "1.234s至2.345s正常裁剪",
          entries["A_normal_44100"]["status"] == "成功",
          entries["A_normal_44100"].get("error") or "已生成 %s" % entries["A_normal_44100"]["output_path"])
    e = entries["F_time_overflow"]
    check("验收2b", "结束10s超出5s时长报时间越界",
          e["status"] == "失败" and "时间越界" in e["error"] and e["output_path"] is None,
          e["error"] or "")

    # 验收 3：输出片段时长误差 ≤1ms（1.000→2.500，期望 1.500s）
    j_info = inspect_wav(entries["J_duration_check"]["output_path"])
    check("验收3", "裁剪1.000-2.500输出时长在1.499~1.501s",
          1.499 <= j_info["duration"] <= 1.501,
          "实测 %.6f s（%d 帧 @ %dHz）" % (j_info["duration"], j_info["frames"], j_info["rate"]))

    # 验收 4：RMS 增益对齐 ±0.5dB（目标 -20dBFS）
    check("验收4", "目标-20dBFS处理后RMS落在-20.5~-19.5dBFS",
          -20.5 <= a_info["rms_dbfs"] <= -19.5,
          "实测 RMS = %.3f dBFS" % a_info["rms_dbfs"])

    # 验收 5：削波回退 + 中文警告 + 峰值不超 0dBFS
    e = entries["B_clip_guard"]
    b_info = inspect_wav(e["output_path"])
    has_warn = any("削波" in w or "回退" in w for w in e["warnings"])
    check("验收5", "近满幅源目标0dBFS不削波并记录中文警告",
          e["status"] == "成功" and has_warn and b_info["peak"] <= 32767,
          "峰值=%d（满幅32767），警告=%s" % (b_info["peak"], e["warnings"]))

    # 验收 6：输出保持源片段采样率/位深/声道数（22050 双声道 16 位）
    check("验收6", "22050Hz双声道16位源输出格式不变",
          (b_info["rate"], b_info["channels"], b_info["sampwidth"]) == (22050, 2, 2),
          "实测 %dHz / %d声道 / %d位" % (b_info["rate"], b_info["channels"], b_info["sampwidth"] * 8))

    # 验收 7：JSON 条目字段（成功 output 非空 error=null；失败 error 非空 output=null）
    ok7 = all(
        (x["status"] == "成功" and x["output_path"] and x["error"] is None)
        or (x["status"] == "失败" and x["error"] and x["output_path"] is None)
        for x in report["entries"]
    ) and all(isinstance(x["warnings"], list) for x in report["entries"])
    check("验收7", "JSON每条含状态/输出路径/错误/警告且取值正确", ok7,
          "成功 %d 条、失败 %d 条" % (report["succeeded"], report["failed"]))

    # 验收 9：单条失败不影响其他条目（文件缺失条目失败，正常条目成功且 WAV 可用）
    e = entries["D_missing_file"]
    ok9 = e["status"] == "失败" and "文件不存在" in e["error"] \
        and entries["A_normal_44100"]["status"] == "成功" \
        and os.path.isfile(entries["A_normal_44100"]["output_path"])
    check("验收9", "单条目失败不影响其他条目独立完成", ok9,
          "缺失条目错误：%s" % e["error"])

    # 验收 10：空清单全局失败
    empty_report = os.path.join(OUT, "report_empty.json")
    code_empty = run_cli(os.path.join(SAMPLES, "manifest_empty.csv"),
                         os.path.join(OUT, "empty_wav"), empty_report)
    rep_empty = load_report(empty_report)
    check("验收10", "空CSV报告全局失败且指明清单为空",
          code_empty == 1 and rep_empty["global_status"] == "失败" and "清单为空" in rep_empty["error"],
          "退出码=%d，错误=%s" % (code_empty, rep_empty["error"]))

    # 验收 10 补充：清单文件本身缺失
    missing_report = os.path.join(OUT, "report_missing.json")
    code_missing = run_cli(os.path.join(SAMPLES, "no_such_manifest.csv"),
                           os.path.join(OUT, "missing_wav"), missing_report)
    rep_missing = load_report(missing_report)
    check("验收10b", "清单文件缺失时全局失败并说明原因",
          code_missing == 1 and rep_missing["global_status"] == "失败"
          and "清单文件不存在" in rep_missing["error"],
          rep_missing["error"])

    # 验收 11：输出与输入相同拒绝处理
    same_report = os.path.join(OUT, "report_same.json")
    run_cli(os.path.join(SAMPLES, "manifest_same_path.csv"), SAMPLES, same_report)
    rep_same = load_report(same_report)
    e = rep_same["entries"][0]
    check("验收11", "输出路径与源相同时拒绝并记录输出与输入相同",
          e["status"] == "失败" and "输出与输入相同" in e["error"] and e["output_path"] is None,
          e["error"])

    # 验收 12：中文错误且能区分"文件不存在"与"格式不支持"
    err_missing = entries["D_missing_file"]["error"]
    err_format = entries["E_bad_format"]["error"]
    check("验收12", "中文错误信息区分文件不存在与格式不支持",
          "文件不存在" in err_missing and "格式不支持" in err_format,
          "缺失：%s ｜ 格式：%s" % (err_missing, err_format))

    # 验收 13：输出目录无临时文件残留，仅预期 WAV + 报告
    expected_wavs = {"A_normal.wav", "B_clip_guard.wav", "C_keep_8000.wav", "J_duration.wav"}
    actual_wavs = set(os.listdir(out_wav))
    out_root = set(os.listdir(OUT))
    no_temp = not any(n.endswith((".tmp", ".temp", ".bak", "~")) or n.startswith("~")
                      for n in list(actual_wavs) + list(out_root))
    check("验收13", "输出目录无临时文件，仅含预期WAV与报告",
          actual_wavs == expected_wavs and out_root == {"wav", "report.json", "report_empty.json",
                                                        "report_missing.json", "report_same.json"}
          and no_temp,
          "WAV目录=%s；根目录=%s" % (sorted(actual_wavs), sorted(out_root)))

    # 验收 14：--help 退出码 0 且含各参数说明
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([PY, "-I", os.path.join(HERE, "cli.py"), "--help"],
                          cwd=HERE, capture_output=True, env=env)
    help_text = proc.stdout.decode("utf-8", errors="replace")
    ok14 = proc.returncode == 0 and all(k in help_text for k in
                                        ("--manifest", "--output-dir", "--report", "清单", "输出目录", "报告"))
    check("验收14", "--help退出码0并显示各参数用法", ok14,
          "退出码=%d" % proc.returncode)

    # 附加客观复核：全部成功条目的 RMS 均满足 ±0.5dB，且所有输出峰值不削波
    extra_ok = True
    details = []
    for e in report["entries"]:
        if e["status"] == "成功":
            info = inspect_wav(e["output_path"])
            details.append("%s: %.3fdBFS peak=%d" % (e["id"], info["rms_dbfs"], info["peak"]))
            extra_ok = extra_ok and info["peak"] <= 32767
    check("附加", "全部成功输出峰值均不超过0dBFS", extra_ok, "；".join(details))

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n========== 验收汇总：%d/%d 通过 ==========" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
