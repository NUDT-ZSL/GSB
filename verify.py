#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动验证 podcast_cutter.py 的 11 条验收要求。

运行方式：python verify.py
先重新生成样本，再对每个场景以子进程方式运行工具并断言报告/输出。
"""
import json
import math
import os
import shutil
import subprocess
import sys
import wave
from array import array

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
OUT = os.path.join(HERE, "out")
SR = 44100
SILENCE_S = 0.020

CHECKS = []


def check(req, desc, ok, detail=""):
    CHECKS.append((req, desc, ok, detail))
    print("[%s] 要求%-2s %s%s" % ("PASS" if ok else "FAIL", req, desc,
                                 (" | " + detail) if detail else ""))


def run_tool(script_name):
    return subprocess.run(
        [sys.executable, "-I", os.path.join(HERE, "podcast_cutter.py"),
         os.path.join(SAMPLES, script_name)],
        capture_output=True, text=True)


def load_report(outdir):
    with open(os.path.join(outdir, "episode_report.json"), encoding="utf-8") as f:
        return json.load(f)


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    a = array("h")
    a.frombytes(raw)
    return sr, a


def rms_dbfs(samples):
    if not samples:
        return float("-inf")
    acc = sum(s * s for s in samples)
    rms = math.sqrt(acc / len(samples)) / 32768.0
    return 20 * math.log10(rms) if rms > 0 else float("-inf")


def peak_dbfs(samples):
    if not samples:
        return float("-inf")
    p = max(max(samples), -min(samples)) / 32768.0
    return 20 * math.log10(p) if p > 0 else float("-inf")


def main():
    # 重新生成样本、清空输出
    subprocess.run([sys.executable, "-I", os.path.join(HERE, "make_samples.py")],
                   check=True, capture_output=True)
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)

    # ============ 场景 A：全部有效（要求 2/3/4/7/8 正向） ============
    r = run_tool("script_ok.json")
    rep = load_report(os.path.join(OUT, "ok"))
    wav_path = os.path.join(OUT, "ok", "episode.wav")

    check(8, "全有效时状态为 success 且无未处理索引",
          rep["status"] == "success" and rep["unprocessed_indices"] == [])

    sr, audio = read_wav(wav_path)
    durs = [1.0, 1.5, 1.0]
    expect = sum(durs) + (len(durs) - 1) * SILENCE_S
    actual = len(audio) / sr
    check(3, "输出总时长=片段和+(N-1)*20ms 且采样率一致",
          sr == SR and abs(actual - expect) < 0.001,
          "期望 %.3fs 实际 %.3fs, sr=%d" % (expect, actual, sr))

    # 逐段核对 RMS 与峰值（要求 4），并核对报告增益
    targets = [-20.0, -18.0, -23.0]
    ok4, detail = True, []
    pos = 0
    for i, (d, t) in enumerate(zip(durs, targets)):
        n = round(d * sr)
        seg = audio[pos:pos + n]
        pos += n + round(SILENCE_S * sr)
        rms, pk = rms_dbfs(seg), peak_dbfs(seg)
        rep_gain = rep["results"][i]["gain_db"]
        src_seg_ok = abs(rms - t) <= 0.5 and pk <= 0.0
        ok4 = ok4 and src_seg_ok and rep_gain is not None
        detail.append("seg%d rms=%.2f(目标%.1f) peak=%.2f gain=%.2f"
                      % (i, rms, t, pk, rep_gain))
    # 静音区间确实为零
    sil = audio[round(1.0 * sr):round(1.0 * sr) + round(SILENCE_S * sr)]
    ok4 = ok4 and all(s == 0 for s in sil)
    check(4, "各段RMS与目标差<=0.5dB、峰值<=0dBFS、报告含逐段增益",
          ok4, "; ".join(detail))

    check(7, "报告为合法JSON，含 results/errors，每片段含 clip/status/duration_s/gain_db",
          isinstance(rep["results"], list) and isinstance(rep["errors"], list)
          and all(all(k in c for k in ("clip", "status", "duration_s", "gain_db"))
                  for c in rep["results"]))

    # ============ 场景 B：部分有效（要求 2/5/6/8/10） ============
    r = run_tool("script_partial.json")
    rep = load_report(os.path.join(OUT, "partial"))
    res = {c["index"]: c for c in rep["results"]}

    check(2, "越界片段被标记索引与原因，有效片段仍产出",
          rep["status"] == "partial" and 1 in rep["unprocessed_indices"]
          and res[1]["status"] == "skipped" and "越界" in res[1]["reason"]
          and res[0]["status"] == "ok" and res[2]["status"] == "ok",
          res[1].get("reason", ""))

    check(5, "目标不可达片段报告原因且不削顶",
          res[4]["status"] == "capped" and "不可达" in res[4]["reason"]
          and res[4]["output_peak_dbfs"] <= 0.0,
          res[4].get("reason", ""))

    check(10, "近乎静音片段判为响度异常且不参与拼接",
          res[3]["status"] == "skipped" and "响度异常" in res[3]["reason"]
          and 3 in rep["unprocessed_indices"],
          res[3].get("reason", ""))

    check(6, "损坏WAV被跳过并标记原因，其余片段成功导出",
          res[5]["status"] == "skipped" and "音频读取失败" in res[5]["reason"]
          and os.path.exists(os.path.join(OUT, "partial", "episode.wav")),
          res[5].get("reason", ""))

    check(8, "部分无效时状态为 partial 且列出全部未处理索引",
          rep["status"] == "partial"
          and sorted(rep["unprocessed_indices"]) == [1, 3, 5])

    # 输出中实际只含 3 个片段（0,2,4），时长核对
    sr, audio = read_wav(os.path.join(OUT, "partial", "episode.wav"))
    expect = (1.0 + 1.0 + 1.0) + 2 * SILENCE_S
    check(3, "部分场景输出时长=3个有效片段+2*20ms",
          abs(len(audio) / sr - expect) < 0.001,
          "期望 %.3fs 实际 %.3fs" % (expect, len(audio) / sr))

    # ============ 场景 C：缺失必填字段（要求 1） ============
    r = run_tool("script_missing_field.json")
    rep = load_report(os.path.join(OUT, "missing"))
    check(1, "缺失必填字段时不生成输出音频，报告指明缺失字段名",
          rep["status"] == "rejected"
          and not os.path.exists(os.path.join(OUT, "missing", "episode.wav"))
          and any("target_loudness" in e for e in rep["errors"]),
          "; ".join(rep["errors"]))

    # ============ 场景 D：非法时间（要求 11） ============
    r = run_tool("script_bad_time.json")
    rep = load_report(os.path.join(OUT, "badtime"))
    check(11, "字符串/负时间剧本被拒绝并列出非法值",
          rep["status"] == "rejected"
          and not os.path.exists(os.path.join(OUT, "badtime", "episode.wav"))
          and any("非数字" in e and "0.5" in e for e in rep["errors"])
          and any("负值" in e and "-0.5" in e for e in rep["errors"]),
          "; ".join(rep["errors"]))

    # ============ 场景 E：全部无效（要求 8） ============
    r = run_tool("script_all_invalid.json")
    rep = load_report(os.path.join(OUT, "allbad"))
    check(8, "全部无效时状态为 failed 且不生成输出",
          rep["status"] == "failed"
          and sorted(rep["unprocessed_indices"]) == [0, 1]
          and not os.path.exists(os.path.join(OUT, "allbad", "episode.wav")))

    # ============ 场景 F：损坏源混合（要求 6 独立剧本） ============
    r = run_tool("script_corrupt.json")
    rep = load_report(os.path.join(OUT, "corrupt"))
    ok6 = (rep["status"] == "partial" and rep["unprocessed_indices"] == [0]
           and os.path.exists(os.path.join(OUT, "corrupt", "episode.wav")))
    if ok6:
        sr, audio = read_wav(os.path.join(OUT, "corrupt", "episode.wav"))
        ok6 = abs(len(audio) / sr - (2.0 + SILENCE_S)) < 0.001
    check(6, "损坏WAV场景：跳过损坏源，其余片段成功导出", ok6)

    # ============ 场景 G：覆盖已有输出（要求 9） ============
    pre = os.path.join(OUT, "ok", "episode.wav")
    with open(pre, "wb") as f:
        f.write(b"OLD-CONTENT-PLACEHOLDER")
    r = run_tool("script_ok.json")
    rep = load_report(os.path.join(OUT, "ok"))
    with open(pre, "rb") as f:
        head = f.read(4)
    check(9, "已存在输出被覆盖且报告记录覆盖行为",
          head == b"RIFF" and rep.get("overwritten") is True
          and any("覆盖" in n for n in rep.get("notes", [])))

    # ============ 汇总 ============
    failed = [c for c in CHECKS if not c[2]]
    print("\n==== 验证汇总: %d 项通过, %d 项失败 ====" % (len(CHECKS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
