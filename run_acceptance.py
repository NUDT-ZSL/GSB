#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收测试：逐条对应任务 15 项要求，全部通过则退出码为 0。"""
import json
import os
import re
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(ROOT, "trim_audio.py")
SAMPLES = os.path.join(ROOT, "samples")
WAV = os.path.join(SAMPLES, "dialog_16k_10s.wav")
RATE = 16000
TOTAL = RATE * 10

RESULTS = []


def run(wav, script, out_wav, out_report, extra=()):
    return subprocess.run(
        [sys.executable, TOOL, wav, script, out_wav, out_report, *extra],
        capture_output=True, text=True, encoding="utf-8",
    )


def script(name):
    return os.path.join(SAMPLES, "scripts", name)


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not ok else ""))


def tmpdir():
    return tempfile.mkdtemp(prefix="trim_acc_")


def read_report(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def wav_params(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate(),
                w.getnframes(), w.readframes(w.getnframes()))


def t01_sample_rate_mismatch():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("sr_mismatch.json"), ow, rp)
    ok = (r.returncode != 0 and "采样率" in r.stderr and "44100" in r.stderr
          and "16000" in r.stderr and not os.path.exists(ow) and not os.path.exists(rp))
    check("01 采样率不一致拒绝执行", ok, r.stderr.strip())


def t02_invalid_interval_skipped():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("mixed_invalid.json"), ow, rp)
    rep = read_report(rp)
    ok = (r.returncode == 0 and rep["applied"] == [[5.0, 8.0]]
          and len(rep["rejected"]) == 1 and rep["rejected"][0]["interval"] == [3, 1]
          and rep["output_frames"] == TOTAL - 3 * RATE and rep["complete"] is False)
    check("02 非法区间不影响有效区间", ok, json.dumps(rep, ensure_ascii=False))


def t03_out_of_bounds_no_cut():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("out_of_bounds.json"), ow, rp)
    rep = read_report(rp)
    same = wav_params(ow)[4] == wav_params(WAV)[4]
    ok = (r.returncode == 0 and same and len(rep["rejected"]) == 1
          and "越界" in rep["rejected"][0]["reason"] and rep["applied"] == []
          and rep["output_frames"] == TOTAL)
    check("03 越界区间不裁音频且单独列出", ok, json.dumps(rep["rejected"], ensure_ascii=False))


def t04_overlap_merged():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("overlap.json"), ow, rp)
    rep = read_report(rp)
    ok = (r.returncode == 0 and rep["output_frames"] == TOTAL - 5 * RATE
          and rep["applied"] == [[1.0, 6.0]])
    check("04 重叠区间合并删减 5 秒", ok, json.dumps(rep["applied"]))


def t05_params_and_frames():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    run(WAV, script("normal.json"), ow, rp)
    oc, os_, orate, oframes, _ = wav_params(ow)
    ic, is_, irate, iframes, _ = wav_params(WAV)
    rep = read_report(rp)
    removed = sum(int((e - s) * RATE) for s, e in rep["applied"])
    ok = ((oc, os_, orate) == (ic, is_, irate) == (1, 2, RATE)
          and oframes == iframes - removed == rep["output_frames"])
    check("05 输出参数一致且帧数正确", ok, f"out={oframes} expect={iframes - removed}")


def t06_bad_scripts():
    d = tmpdir()
    cases = [
        os.path.join(SAMPLES, "scripts", "nonexistent.json"),
        script("broken.json"),
        script("toplevel_array.json"),
    ]
    ok = True
    detail = ""
    for i, sc in enumerate(cases):
        ow = os.path.join(d, f"o{i}.wav"); rp = os.path.join(d, f"r{i}.json")
        r = run(WAV, sc, ow, rp)
        if r.returncode == 0 or os.path.exists(ow):
            ok = False; detail = f"case {i} rc={r.returncode}"
    check("06 缺失/损坏/顶层数组脚本均失败且无输出", ok, detail)


def t07_bad_wav_formats():
    d = tmpdir()
    ok = True; detail = ""
    for i, bad in enumerate(["stereo.wav", "8bit.wav"]):
        ow = os.path.join(d, f"o{i}.wav"); rp = os.path.join(d, f"r{i}.json")
        r = run(os.path.join(SAMPLES, bad), script("normal.json"), ow, rp)
        if r.returncode == 0 or os.path.exists(ow) or "拒绝" not in r.stderr:
            ok = False; detail = f"{bad} rc={r.returncode} err={r.stderr.strip()}"
    leftover = [f for f in os.listdir(d) if f.endswith(".wav")]
    check("07 非16位单声道拒绝且无输出", ok and not leftover, detail or str(leftover))


def t08_report_fields():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("normal.json"), ow, rp)
    rep = read_report(rp)
    types = (isinstance(rep.get("output_wav"), str)
             and isinstance(rep.get("original_frames"), int)
             and isinstance(rep.get("output_frames"), int)
             and isinstance(rep.get("applied"), list)
             and isinstance(rep.get("rejected"), list)
             and isinstance(rep.get("complete"), bool))
    ok = r.returncode == 0 and set(rep) == {
        "output_wav", "original_frames", "output_frames",
        "applied", "rejected", "complete"} and types
    check("08 报告六个固定字段及类型", ok, json.dumps(list(rep)))


def t09_applied_sorted_rejected_reasons():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("mixed.json"), ow, rp)
    rep = read_report(rp)
    starts = [iv[0] for iv in rep["applied"]]
    ok = (r.returncode == 0 and rep["applied"] == [[1.0, 2.0], [5.0, 6.0]]
          and starts == sorted(starts)
          and len(rep["rejected"]) == 2
          and all(isinstance(x.get("reason"), str) and x["reason"] for x in rep["rejected"]))
    check("09 applied 升序且 rejected 含原因", ok, json.dumps(rep, ensure_ascii=False))


def t10_complete_false_keeps_output():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("out_of_bounds.json"), ow, rp)
    rep = read_report(rp)
    playable = False
    try:
        wav_params(ow); playable = True
    except Exception:
        pass
    ok = (r.returncode == 0 and rep["complete"] is False
          and os.path.exists(rep["output_wav"]) and playable)
    check("10 complete=false 时输出仍可播放", ok, "")


def t11_delete_all_zero_frames():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("delete_all.json"), ow, rp)
    rep = read_report(rp)
    frames = wav_params(ow)[3]
    ok = (r.returncode == 0 and frames == 0 and rep["output_frames"] == 0
          and rep["complete"] is True)
    check("11 删除全部输出零帧 WAV 且 complete=true", ok, f"frames={frames}")


def t12_no_overwrite():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    with open(ow, "wb") as f:
        f.write(b"ORIGINAL-CONTENT")
    before = open(ow, "rb").read()
    r = run(WAV, script("normal.json"), ow, rp)
    after = open(ow, "rb").read()
    ok = (r.returncode != 0 and "覆盖" in r.stderr and before == after
          and not os.path.exists(rp))
    check("12 已存在输出拒绝覆盖且内容不变", ok, r.stderr.strip())


def t13_millisecond_precision():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("millisecond.json"), ow, rp)
    rep = read_report(rp)
    # [0.001, 0.002] @16000Hz -> 帧 [16, 32)，裁 16 帧
    ok = (r.returncode == 0 and rep["output_frames"] == TOTAL - 16
          and rep["applied"] == [[0.001, 0.002]])
    check("13 三位小数精确换算帧边界", ok, json.dumps(rep["applied"]))


def t14_summary_matches_report():
    d = tmpdir(); ow = os.path.join(d, "o.wav"); rp = os.path.join(d, "r.json")
    r = run(WAV, script("mixed.json"), ow, rp)
    rep = read_report(rp)
    m = re.search(r"输出帧数=(\d+) 生效区间数=(\d+) 被拒区间数=(\d+)", r.stdout)
    ok = (m is not None
          and int(m.group(1)) == rep["output_frames"]
          and int(m.group(2)) == len(rep["applied"])
          and int(m.group(3)) == len(rep["rejected"]))
    check("14 摘要数字与报告一致", ok, r.stdout.strip())


def t15_report_write_failure_cleans_wav():
    d = tmpdir()
    ow = os.path.join(d, "o.wav")
    rp = os.path.join(d, "no_such_dir", "r.json")  # 父目录不存在，写报告必失败
    r = run(WAV, script("normal.json"), ow, rp)
    ok = (r.returncode != 0 and not os.path.exists(ow)
          and not os.path.exists(ow + ".tmp"))
    check("15 报告写入失败则非零退出且无 WAV 残留", ok, r.stderr.strip())


def main():
    for fn in [t01_sample_rate_mismatch, t02_invalid_interval_skipped,
               t03_out_of_bounds_no_cut, t04_overlap_merged, t05_params_and_frames,
               t06_bad_scripts, t07_bad_wav_formats, t08_report_fields,
               t09_applied_sorted_rejected_reasons, t10_complete_false_keeps_output,
               t11_delete_all_zero_frames, t12_no_overwrite, t13_millisecond_precision,
               t14_summary_matches_report, t15_report_write_failure_cleans_wav]:
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f"异常: {e!r}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n通过 {passed}/{len(RESULTS)}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
