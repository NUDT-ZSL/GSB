# -*- coding: utf-8 -*-
"""端到端验收脚本（仅标准库）：逐条核对任务 11 项验收要求。

流程：
  1. 调用 make_samples.py 在独立目录生成确定性测试数据；
  2. 以子进程方式运行 voice_clip_tool.py（python -I，隔离模式）；
  3. 读取输出 WAV 与 UTF-8 报告，校验时长、RMS、状态、顺序、退出码与提示语。

用法：
  python -I tests/verify.py [结果留档路径，可选]
"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "voice_clip_tool.py")
MAKE = os.path.join(ROOT, "tests", "make_samples.py")

SAMPLE_RATE = 16000
GAP_MS = 50
GAP_FRAMES = SAMPLE_RATE * GAP_MS // 1000  # 800
SEG_FRAMES = 8000
TARGET_DBFS = -20.0
FULL_SCALE = 32768.0

LINE_RE = re.compile(
    r"^\[(?P<idx>\d+)\] (?P<id>[^|]+?) \| (?P<status>[^|]+?) \| "
    r"(?P<rms>null|-?\d+\.\d+) \| (?P<gain>null|-?\d+\.\d+) \| (?P<note>.*)$"
)


def run_subprocess(args):
    """运行子进程并兼容中文 Windows 的 GBK 控制台输出。"""
    proc = subprocess.run(args, capture_output=True)
    def decode(b):
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return b.decode("gbk", errors="replace")
    proc.stdout = decode(proc.stdout)
    proc.stderr = decode(proc.stderr)
    return proc


class CheckFailure(AssertionError):
    pass


def run_tool(test_dir, wav_name, config_name, out_tag):
    wav = os.path.join(test_dir, wav_name)
    cfg = os.path.join(test_dir, config_name)
    out_wav = os.path.join(test_dir, "out_%s.wav" % out_tag)
    out_report = os.path.join(test_dir, "report_%s.txt" % out_tag)
    proc = run_subprocess(
        [sys.executable, "-I", SRC,
         "--wav", wav, "--config", cfg,
         "--out-wav", out_wav, "--out-report", out_report],
    )
    return proc, out_wav, out_report


def read_wav(path):
    with wave.open(path, "rb") as wf:
        params = wf.getparams()
        raw = wf.readframes(wf.getnframes())
    samples = list(struct.unpack("<%dh" % params.nframes, raw))
    return params, samples


def rms_dbfs(samples):
    if not samples:
        return None
    mean_sq = sum(s * s for s in samples) / len(samples)
    if mean_sq <= 0:
        return None
    return 20.0 * math.log10(math.sqrt(mean_sq) / FULL_SCALE)


def parse_report(path):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    entries = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            entries.append(m.groupdict())
    return text, entries


def main():
    results = []  # (编号, 是否通过, 说明)

    def check(num, name, fn):
        try:
            detail = fn()
            results.append((num, True, name, detail))
        except CheckFailure as exc:
            results.append((num, False, name, str(exc)))
        except Exception as exc:  # noqa: BLE001 - 验收脚本需捕获全部异常并汇总
            results.append((num, False, name, "意外异常: %r" % exc))

    with tempfile.TemporaryDirectory(prefix="voice_test_") as test_dir:
        # 生成确定性样本
        gen = run_subprocess(
            [sys.executable, "-I", MAKE, test_dir],
        )
        if gen.returncode != 0:
            print("样本生成失败:\n" + gen.stderr)
            return 3

        # ---------- 正常主流程 ----------
        proc, out_wav, out_report = run_tool(test_dir, "voice_mono.wav",
                                             "config_ok.json", "ok")

        def c1():
            if proc.returncode != 0:
                raise CheckFailure("退出码=%d，stderr=%s" % (proc.returncode, proc.stderr))
            if not (os.path.isfile(out_wav) and os.path.isfile(out_report)):
                raise CheckFailure("未同时生成 WAV 与报告")
            text, entries = parse_report(out_report)
            if len(entries) != 6:
                raise CheckFailure("报告条目数=%d，期望 6" % len(entries))
            return "退出码 0；报告含全部 6 个片段条目"
        check(1, "有效输入退出码0且报告含全部片段", c1)

        text, entries = parse_report(out_report)
        params, merged = read_wav(out_wav)
        by_id = {e["id"]: e for e in entries}

        def c2():
            for sid in ("seg-out-of-range", "seg-reversed"):
                if by_id[sid]["status"] != "区间无效":
                    raise CheckFailure("%s 状态=%s" % (sid, by_id[sid]["status"]))
                if by_id[sid]["rms"] != "null" or by_id[sid]["gain"] != "null":
                    raise CheckFailure("%s 的 RMS/增益应为 null" % sid)
            if by_id["seg-normal"]["status"] != "已平衡":
                raise CheckFailure("有效片段未正常处理: %s" % by_id["seg-normal"]["status"])
            return "2 条越界/逆序片段标“区间无效”，其余片段正常处理"
        check(2, "越界与非法区间标“区间无效”且不影响其他片段", c2)

        def c3():
            if by_id["seg-silence"]["status"] != "静音":
                raise CheckFailure("状态=%s" % by_id["seg-silence"]["status"])
            if by_id["seg-silence"]["rms"] != "null" or by_id["seg-silence"]["gain"] != "null":
                raise CheckFailure("静音片段 RMS/增益应为 null")
            # 保留片段: normal / quiet / loud 共 3 个
            expected_frames = SEG_FRAMES * 3 + GAP_FRAMES * 2
            if params.nframes != expected_frames:
                raise CheckFailure("合并帧数=%d，期望 %d" % (params.nframes, expected_frames))
            return "静音片段不合并；输出 %d 帧（不含静音片段时长）" % params.nframes
        check(3, "全零片段标“静音”且合并 WAV 不含其时长", c3)

        # 合并布局: [normal 8000][gap 800][quiet 8000][gap 800][loud 8000]
        normal_out = merged[0:SEG_FRAMES]
        gap1 = merged[SEG_FRAMES:SEG_FRAMES + GAP_FRAMES]
        quiet_out = merged[SEG_FRAMES + GAP_FRAMES:2 * SEG_FRAMES + GAP_FRAMES]
        gap2 = merged[2 * SEG_FRAMES + GAP_FRAMES:2 * SEG_FRAMES + 2 * GAP_FRAMES]
        loud_out = merged[2 * SEG_FRAMES + 2 * GAP_FRAMES:]

        def c4():
            db = rms_dbfs(normal_out)
            if db is None or abs(db - TARGET_DBFS) > 1.0:
                raise CheckFailure("normal 输出 RMS=%.2f dBFS，与 -20 偏差超过 1 dB" % db)
            if by_id["seg-normal"]["status"] != "已平衡":
                raise CheckFailure("状态=%s" % by_id["seg-normal"]["status"])
            return "normal 输出 RMS=%.2f dBFS（偏差 %.2f dB）" % (db, abs(db - TARGET_DBFS))
        check(4, "未受限片段输出 RMS 与 -20 dBFS 偏差 ≤1 dB", c4)

        def c5():
            e = by_id["seg-quiet"]
            if e["status"] != "增益受限":
                raise CheckFailure("状态=%s" % e["status"])
            if e["gain"] != "20.0":
                raise CheckFailure("报告增益=%s，期望 20.0" % e["gain"])
            orig = float(e["rms"])
            if not (-47.5 <= orig <= -44.5):
                raise CheckFailure("原始 RMS=%.2f，期望约 -46 dBFS" % orig)
            # 施加 +20 dB 后仍远低于目标，确认确实受限
            out_db = rms_dbfs(quiet_out)
            if out_db is None or out_db >= TARGET_DBFS:
                raise CheckFailure("受限后 RMS=%.2f 不应达到目标" % out_db)
            return "原始 RMS=%.2f dBFS，增益=20.0 dB，输出 RMS=%.2f dBFS" % (
                orig, out_db)
        check(5, "极低响度片段标“增益受限”且增益为 20.0", c5)

        def c6():
            e = by_id["seg-normal"]
            if e["status"] != "已平衡":
                raise CheckFailure("状态=%s" % e["status"])
            reported_gain = float(e["gain"])
            orig = float(e["rms"])
            expected_gain = round(TARGET_DBFS - orig, 1)
            if abs(reported_gain - expected_gain) > 0.11:
                raise CheckFailure("报告增益 %.1f 与按原始 RMS 推算 %.1f 不一致"
                                   % (reported_gain, expected_gain))
            out_db = rms_dbfs(normal_out)
            if abs(out_db - (orig + reported_gain)) > 1.0:
                raise CheckFailure("输出 RMS %.2f 与 原始RMS+增益 不符" % out_db)
            return "报告增益 %.1f dB 与输出 RMS(%.2f dBFS) 一致" % (reported_gain, out_db)
        check(6, "正常片段标“已平衡”且增益与输出 RMS 一致", c6)

        def c7():
            e = by_id["seg-loud"]
            if e["status"] != "可能削波":
                raise CheckFailure("状态=%s" % e["status"])
            if len(loud_out) != SEG_FRAMES:
                raise CheckFailure("合并输出缺少削波片段，帧数=%d" % len(loud_out))
            return "接近满幅片段标“可能削波”并完整参与合并（%d 帧）" % len(loud_out)
        check(7, "接近满幅片段标“可能削波”且仍参与合并", c7)

        def c8():
            if params.nchannels != 1 or params.sampwidth != 2:
                raise CheckFailure("输出不是单声道16位PCM: %s" % (params,))
            if any(gap1) or any(gap2):
                raise CheckFailure("片段间 50ms 间隔不是纯静音")
            retained = 3
            expected = SEG_FRAMES * retained + GAP_FRAMES * (retained - 1)
            if params.nframes != expected:
                raise CheckFailure("时长帧数=%d，期望 %d" % (params.nframes, expected))
            duration_ms = params.nframes / SAMPLE_RATE * 1000
            return "输出单声道16位PCM；时长 %.1f ms = 3×500 + 2×50" % duration_ms
        check(8, "拼接顺序、50ms 静音间隔与输出 WAV 时长正确", c8)

        def c9():
            expected_order = [
                "seg-normal", "seg-silence", "seg-quiet", "seg-loud",
                "seg-out-of-range", "seg-reversed",
            ]
            actual = [e["id"] for e in entries]
            if actual != expected_order:
                raise CheckFailure("报告顺序=%s" % actual)
            with open(out_report, "rb") as f:
                raw = f.read()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise CheckFailure("报告带 BOM")
            raw.decode("utf-8")  # 非 UTF-8 会抛异常
            for sid in ("seg-silence", "seg-out-of-range", "seg-reversed"):
                e = by_id[sid]
                if e["rms"] != "null" or e["gain"] != "null":
                    raise CheckFailure("%s 的 RMS/增益应为 null" % sid)
            return "顺序与配置一致；UTF-8 无 BOM；无效/静音片段两字段为 null"
        check(9, "UTF-8 报告顺序正确且 null 字段符合要求", c9)

        # ---------- 错误路径 ----------
        def expect_config_error(cfg_name, tag):
            d = os.path.join(test_dir, "err_%s" % tag)
            os.makedirs(d, exist_ok=True)
            w = os.path.join(d, "o.wav")
            r = os.path.join(d, "r.txt")
            p = run_subprocess(
                [sys.executable, "-I", SRC,
                 "--wav", os.path.join(test_dir, "voice_mono.wav"),
                 "--config", os.path.join(test_dir, cfg_name),
                 "--out-wav", w, "--out-report", r])
            if p.returncode != 1:
                raise CheckFailure("退出码=%d，期望 1" % p.returncode)
            if "配置文件错误" not in p.stderr:
                raise CheckFailure("stderr 缺少“配置文件错误”: %s" % p.stderr)
            if os.path.exists(w) or os.path.exists(r):
                raise CheckFailure("错误时仍生成了输出文件")
            return "退出码 1，stderr 含“配置文件错误”，无输出文件"

        def c10():
            r1 = expect_config_error("config_broken.json", "broken")
            r2 = expect_config_error("config_missing_field.json", "missing")
            return r1 + "；缺字段场景同样通过"
        check(10, "损坏 JSON / 缺字段：退出码1、指定提示、无输出", c10)

        def c11():
            d = os.path.join(test_dir, "err_audio")
            os.makedirs(d, exist_ok=True)
            o_wav, o_rep = os.path.join(d, "o.wav"), os.path.join(d, "r.txt")
            # 立体声
            p = run_subprocess(
                [sys.executable, "-I", SRC,
                 "--wav", os.path.join(test_dir, "voice_stereo.wav"),
                 "--config", os.path.join(test_dir, "config_ok.json"),
                 "--out-wav", o_wav, "--out-report", o_rep])
            if p.returncode != 2 or "不支持的音频格式" not in p.stderr:
                raise CheckFailure("立体声: 退出码=%d stderr=%s" % (p.returncode, p.stderr))
            if os.path.exists(o_wav) or os.path.exists(o_rep):
                raise CheckFailure("立体声错误时仍生成了输出文件")
            # 不存在的 WAV
            p2 = run_subprocess(
                [sys.executable, "-I", SRC,
                 "--wav", os.path.join(test_dir, "no_such.wav"),
                 "--config", os.path.join(test_dir, "config_ok.json"),
                 "--out-wav", o_wav, "--out-report", o_rep])
            if p2.returncode != 2 or "不支持的音频格式" not in p2.stderr:
                raise CheckFailure("缺失文件: 退出码=%d" % p2.returncode)
            if os.path.exists(o_wav) or os.path.exists(o_rep):
                raise CheckFailure("文件缺失时仍生成了输出文件")
            return "立体声与缺失 WAV 均退出码 2，含“不支持的音频格式”，无输出"
        check(11, "立体声/缺失 WAV：退出码2、指定提示、无输出", c11)

    # ---------------- 汇总 ----------------
    width = max(len(name) for _, _, name, _ in results)
    lines = ["=" * 78]
    passed = 0
    for num, ok, name, detail in results:
        flag = "PASS" if ok else "FAIL"
        lines.append("验收%2d [%s] %s" % (num, flag, name.ljust(width)))
        lines.append("        %s" % detail)
        if ok:
            passed += 1
    lines.append("=" * 78)
    lines.append("结果: %d/%d 通过" % (passed, len(results)))
    output = "\n".join(lines)
    print(output)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8", newline="\n") as f:
            f.write(output + "\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
