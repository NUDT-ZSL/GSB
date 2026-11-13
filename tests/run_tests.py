#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_tests.py
============

loudness_normalizer.py 的验收测试，覆盖任务书 15 条要求。
仅使用标准库；确定性样本由 make_samples.py 生成到临时工作区。

运行：
    python tests/run_tests.py
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(PROJECT_ROOT, "loudness_normalizer.py")
MAKE_SAMPLES = os.path.join(PROJECT_ROOT, "make_samples.py")

sys.path.insert(0, PROJECT_ROOT)
import loudness_normalizer as ln  # noqa: E402  白盒测试稳定排序

PASS = "PASS"
FAIL = "FAIL"
results = []


def check(tid, title, condition, detail=""):
    results.append((tid, title, bool(condition), detail))
    mark = PASS if condition else FAIL
    print("[%s] %s %s%s" % (mark, tid, title,
                            (" -- " + detail) if detail and not condition else ""))


def run_tool(input_wav, out_dir, use_cmd=False):
    if use_cmd:
        # 显式经 Windows cmd 调用，证明在 cmd 下可直接运行
        cmd = 'python "%s" "%s" "%s"' % (SCRIPT, input_wav, out_dir)
        return subprocess.run(cmd, capture_output=True, text=True,
                              shell=True, cwd=PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, SCRIPT, input_wav, out_dir],
        capture_output=True, text=True, cwd=PROJECT_ROOT)


def report_path(out_dir, base):
    return os.path.join(out_dir, base + "_report.json")


def wav_path(out_dir, base):
    return os.path.join(out_dir, base + "_normalized.wav")


def load_report(out_dir, base):
    with open(report_path(out_dir, base), "r", encoding="utf-8") as fp:
        return json.load(fp)


def read_wav(path):
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        import array
        samples = array.array("h")
        samples.frombytes(wf.readframes(params.nframes))
    return params, samples


def rms(values):
    if not values:
        return 0.0
    return (sum(v * v for v in values) / len(values)) ** 0.5 / 32768.0


def main():
    workspace = tempfile.mkdtemp(prefix="ln_test_")
    sample_dir = os.path.join(workspace, "samples")
    try:
        # 1) 生成确定性样本
        gen = subprocess.run([sys.executable, MAKE_SAMPLES, sample_dir],
                             capture_output=True, text=True)
        check("SETUP", "deterministic sample generation", gen.returncode == 0,
              gen.stderr)

        names = {
            "basic": "sample_44k_silence_speech.wav",
            "two": "sample_44k_two_speech.wav",
            "clamp": "sample_44k_gain_clamp.wav",
            "silence": "sample_44k_all_silence.wav",
            "short": "sample_44k_short_speech.wav",
            "bad8k": "sample_8k.wav",
        }
        p = {k: os.path.join(sample_dir, v) for k, v in names.items()}

        # ---- T1: 44.1k 正常；8k 报 error、无 WAV、退出码非零 ----
        out_ok = os.path.join(workspace, "out_basic")
        r = run_tool(p["basic"], out_ok)
        base = os.path.splitext(names["basic"])[0]
        check("T1a", "44.1kHz sample processed, exit 0",
              r.returncode == 0 and os.path.exists(wav_path(out_ok, base))
              and os.path.exists(report_path(out_ok, base)),
              r.stderr)

        out_8k = os.path.join(workspace, "out_8k")
        r8 = run_tool(p["bad8k"], out_8k)
        base8 = os.path.splitext(names["bad8k"])[0]
        rep8 = load_report(out_8k, base8)
        check("T1b", "8kHz sample: nonzero exit, error report, no output WAV",
              r8.returncode != 0 and "error" in rep8
              and not os.path.exists(wav_path(out_8k, base8)),
              "rc=%s report=%s" % (r8.returncode, rep8))

        # ---- T2: 静音段不调整，增益 1.0 ----
        rep_basic = load_report(out_ok, base)
        silence_segs = [s for s in rep_basic["segments"] if s["type"] == "silence"]
        speech_segs = [s for s in rep_basic["segments"] if s["type"] == "speech"]
        check("T2", "silence segments keep gain 1.0",
              len(silence_segs) >= 1 and all(s["gain"] == 1.0 for s in silence_segs)
              and all(s["adjusted"] is False for s in silence_segs),
              json.dumps(silence_segs))

        # ---- T3: 中位数目标 RMS，0.1/0.2 -> 0.15 ----
        out_two = os.path.join(workspace, "out_two")
        r = run_tool(p["two"], out_two)
        base_two = os.path.splitext(names["two"])[0]
        rep_two = load_report(out_two, base_two)
        check("T3", "target_rms is median of speech RMS (~0.15)",
              r.returncode == 0 and round(rep_two["target_rms"], 3) == 0.15,
              "target_rms=%r" % rep_two["target_rms"])

        # ---- T4: 需 3x 的段增益截断 2.0，最终 RMS 约 0.2 ----
        out_clamp = os.path.join(workspace, "out_clamp")
        r = run_tool(p["clamp"], out_clamp)
        base_c = os.path.splitext(names["clamp"])[0]
        rep_c = load_report(out_clamp, base_c)
        first_speech = next(s for s in rep_c["segments"] if s["type"] == "speech")
        params_c, out_samples_c = read_wav(wav_path(out_clamp, base_c))
        i0 = int(round(first_speech["start"] * 44100))
        i1 = int(round(first_speech["end"] * 44100))
        out_rms_first = rms(out_samples_c[i0:i1])
        check("T4", "3x-needed segment gain clamped to 2.0, output RMS ~0.2",
              r.returncode == 0 and first_speech["gain"] == 2.0
              and abs(out_rms_first - 0.2) < 0.01,
              "gain=%r out_rms=%.4f" % (first_speech["gain"], out_rms_first))

        # ---- T5: 溢出截断，输出不超过 32767 / 不低于 -32768 ----
        params_in_c, in_samples_c = read_wav(p["clamp"])
        check("T5", "samples clipped to signed 16-bit bounds (32767 reached)",
              max(out_samples_c) <= 32767 and min(out_samples_c) >= -32768
              and max(out_samples_c) == 32767 and max(in_samples_c) == 32000,
              "in_max=%d out_max=%d out_min=%d"
              % (max(in_samples_c), max(out_samples_c), min(out_samples_c)))

        # ---- T6: 输出 WAV 头与帧数与输入一致 ----
        params_in, in_samples = read_wav(p["basic"])
        params_out, out_samples = read_wav(wav_path(out_ok, base))
        check("T6", "output WAV keeps mono/16bit/44.1k and same frame count",
              params_out.nchannels == params_in.nchannels == 1
              and params_out.sampwidth == params_in.sampwidth == 2
              and params_out.framerate == params_in.framerate == 44100
              and params_out.comptype == "NONE"
              and params_out.nframes == params_in.nframes
              and len(out_samples) == len(in_samples),
              "in=%s out=%s" % (params_in, params_out))

        # ---- T7: 报告字段齐全，时间 3 位小数精度 ----
        required_fields = {"start", "end", "original_rms", "target_rms", "gain"}
        fields_ok = all(required_fields.issubset(s.keys())
                        for s in rep_basic["segments"])
        times_ok = all(abs(round(s["start"], 3) - s["start"]) < 1e-12
                       and abs(round(s["end"], 3) - s["end"]) < 1e-12
                       for s in rep_basic["segments"])
        check("T7", "each segment has start/end/original_rms/target_rms/gain, "
                    "times at 3-decimal precision",
              fields_ok and times_ok, json.dumps(rep_basic["segments"][0]))

        # ---- T8: start 升序、同 start 稳定排序 ----
        ordered = sorted(rep_basic["segments"], key=lambda s: s["start"])
        starts = [s["start"] for s in rep_basic["segments"]]
        whitebox = [{"start": 1.0, "id": "a"}, {"start": 0.5, "id": "b"},
                    {"start": 1.0, "id": "c"}, {"start": 0.5, "id": "d"}]
        sorted_wb = ln.sort_segments_by_start(whitebox)
        stable_ok = [x["id"] for x in sorted_wb] == ["b", "d", "a", "c"]
        check("T8", "segments sorted by start ascending, stable on ties",
              starts == [s["start"] for s in ordered] and stable_ok,
              str(starts))

        # ---- T9: 全静音 -> 逐字节相同 + message ----
        out_sil = os.path.join(workspace, "out_silence")
        r = run_tool(p["silence"], out_sil)
        base_s = os.path.splitext(names["silence"])[0]
        rep_s = load_report(out_sil, base_s)
        with open(p["silence"], "rb") as f1, \
                open(wav_path(out_sil, base_s), "rb") as f2:
            byte_same = f1.read() == f2.read()
        check("T9", "all-silence: byte-identical WAV and 'no speech detected'",
              r.returncode == 0 and byte_same
              and rep_s["message"] == "no speech detected"
              and all(s["gain"] == 1.0 for s in rep_s["segments"]),
              rep_s.get("message"))

        # ---- T10: 输入不存在 -> error + 非零退出 ----
        missing = os.path.join(workspace, "does_not_exist.wav")
        out_miss = os.path.join(workspace, "out_missing")
        r = run_tool(missing, out_miss)
        rep_miss = load_report(out_miss, "does_not_exist")
        check("T10", "missing input: nonzero exit, report contains error",
              r.returncode != 0 and "error" in rep_miss
              and not os.path.exists(wav_path(out_miss, "does_not_exist")),
              "rc=%d report=%s" % (r.returncode, rep_miss))

        # 无法解析的“假 WAV”
        fake = os.path.join(workspace, "fake.wav")
        with open(fake, "wb") as fp:
            fp.write(b"NOT A REAL WAV FILE CONTENT")
        out_fake = os.path.join(workspace, "out_fake")
        r = run_tool(fake, out_fake)
        rep_fake = load_report(out_fake, "fake")
        check("T10b", "unparseable WAV: nonzero exit, report contains error",
              r.returncode != 0 and "error" in rep_fake,
              "rc=%d report=%s" % (r.returncode, rep_fake))

        # ---- T11: 输出路径不可写（指向已存在文件）-> 不崩溃、error、非零 ----
        collision_file = os.path.join(workspace, "a_file_not_dir.txt")
        with open(collision_file, "w") as fp:
            fp.write("occupied")
        r = run_tool(p["basic"], collision_file)
        fallback = os.path.join(sample_dir, base + "_report.json")
        fb_ok = False
        if os.path.exists(fallback):
            with open(fallback, encoding="utf-8") as fp:
                fb_ok = "error" in json.load(fp)
        check("T11", "unwritable output target: no crash, error report, "
                     "nonzero exit (fallback report written)",
              r.returncode != 0 and fb_ok
              and open(collision_file).read() == "occupied",
              "rc=%d fallback=%s" % (r.returncode, fb_ok))

        # ---- T12: 输出目录不存在时自动创建 ----
        nested = os.path.join(workspace, "nested", "level2", "level3")
        r = run_tool(p["basic"], nested)
        check("T12", "nonexistent output directory auto-created with both outputs",
              r.returncode == 0 and os.path.isdir(nested)
              and os.path.exists(wav_path(nested, base))
              and os.path.exists(report_path(nested, base)), r.stderr)

        # ---- T13: 短于 0.1s 的语音段 gain=1.0 ----
        out_short = os.path.join(workspace, "out_short")
        r = run_tool(p["short"], out_short)
        base_sh = os.path.splitext(names["short"])[0]
        rep_sh = load_report(out_short, base_sh)
        short_seg = next(s for s in rep_sh["segments"]
                         if s["type"] == "speech" and s["end"] - s["start"] < 0.1)
        params_sh, out_sh_samples = read_wav(wav_path(out_short, base_sh))
        _, in_sh_samples = read_wav(p["short"])
        j0, j1 = int(round(short_seg["start"] * 44100)), \
            int(round(short_seg["end"] * 44100))
        untouched = list(out_sh_samples[j0:j1]) == list(in_sh_samples[j0:j1])
        check("T13", "speech segment shorter than 0.1s kept untouched, gain 1.0",
              r.returncode == 0 and short_seg["gain"] == 1.0
              and short_seg["adjusted"] is False and untouched,
              json.dumps(short_seg))

        # ---- T14: adjusted_segments / average_gain 仅统计实际调整段 ----
        adjusted = [s for s in rep_sh["segments"] if s["adjusted"]]
        check("T14", "adjusted_segments=1 and average_gain equals that gain",
              rep_sh["adjusted_segments"] == 1 and len(adjusted) == 1
              and abs(rep_sh["average_gain"] - adjusted[0]["gain"]) < 1e-9
              and abs(rep_sh["average_gain"] - 1.5) < 0.01,
              "count=%r avg=%r" % (rep_sh["adjusted_segments"],
                                   rep_sh["average_gain"]))
        # 全静音时 average_gain 为 null、adjusted_segments 为 0
        check("T14b", "no adjusted segments -> count 0, average_gain null",
              rep_s["adjusted_segments"] == 0 and rep_s["average_gain"] is None)

        # ---- T15: 仅标准库，且经 cmd 直接运行 ----
        with open(SCRIPT, encoding="utf-8") as fp:
            tree = ast.parse(fp.read())
        allowed = {"argparse", "array", "json", "math", "os", "statistics",
                   "sys", "time", "wave"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        r_cmd = run_tool(p["basic"], os.path.join(workspace, "out_cmd"))
        check("T15", "stdlib-only imports and runnable directly from Windows cmd",
              imported.issubset(allowed) and r_cmd.returncode == 0,
              "imports=%s rc=%d" % (sorted(imported), r_cmd.returncode))

    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    failed = [r for r in results if not r[2]]
    print("\n==== %d checks, %d passed, %d failed ===="
          % (len(results), len(results) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
