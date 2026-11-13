# -*- coding: utf-8 -*-
"""
verify_all.py — 对 wav_joiner.py 做端到端验收（覆盖任务 10 条验收标准）。

仅使用标准库；以独立实现的 RIFF 解析逻辑复核输出，不直接信任被测代码。
运行:
    python -I make_samples.py demo
    python -I verify_all.py
全部通过时退出码 0，任一失败退出码 1。
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wav_joiner  # noqa: E402  仅用于 inspect_wav / 浮点转换的异常路径测试

# Windows 控制台默认可能是 GBK 代码页，统一强制 UTF-8，保证中文输出可读
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")
MAT = os.path.join(ROOT, "materials")
INT32_MAX = 2_147_483_647
INT32_MIN = -2_147_483_648

_passed = 0
_failed = 0
_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        _failures.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------------- #
# 独立 RIFF/WAVE 解析（与 wav_joiner 的实现相互独立，用于交叉验证）
# --------------------------------------------------------------------------- #
def read_wav(path: str) -> dict:
    with open(path, "rb") as f:
        blob = f.read()
    assert blob[0:4] == b"RIFF" and blob[8:12] == b"WAVE", "非 RIFF/WAVE"
    assert struct.unpack_from("<I", blob, 4)[0] + 8 == len(blob), "RIFF 长度不符"
    offset, fmt, data = 12, None, None
    while offset + 8 <= len(blob):
        cid, size = struct.unpack_from("<4sI", blob, offset)
        body = blob[offset + 8 : offset + 8 + size]
        assert len(body) == size, f"{cid} 块被截断"
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        offset += 8 + size + (size & 1)
    assert fmt is not None and data is not None
    tag, ch, rate, _br, ba, bits = struct.unpack("<HHIIHH", fmt[:16])
    return {
        "tag": tag,
        "channels": ch,
        "rate": rate,
        "bits": bits,
        "block_align": ba,
        "data": data,
        "frames": len(data) // ba,
    }


def independent_float_to_int32(data: bytes) -> bytes:
    """验证脚本自带的浮点->整数转换，用于逐字节比对拼接结果。"""
    out = bytearray()
    for i in range(len(data) // 4):
        (x,) = struct.unpack_from("<f", data, i * 4)
        if math.isnan(x):
            n = 0
        elif x >= 1.0:
            n = INT32_MAX
        elif x <= -1.0:
            n = INT32_MIN
        else:
            n = int(math.copysign(math.floor(abs(x) * INT32_MAX + 0.5), x))
        out += struct.pack("<i", n)
    return bytes(out)


def run_joiner(manifest: str, out_dir: str) -> subprocess.CompletedProcess:
    os.makedirs(out_dir, exist_ok=True)
    return subprocess.run(
        [sys.executable, "-I",
         os.path.join(os.path.dirname(os.path.abspath(__file__)), "wav_joiner.py"),
         os.path.join(ROOT, manifest), out_dir],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def report_lines(out_dir: str) -> list[str]:
    with open(os.path.join(out_dir, "report.txt"), "r", encoding="utf-8") as f:
        return f.read().splitlines()


def parse_records(out_dir: str) -> list[dict]:
    recs = []
    for line in report_lines(out_dir):
        if not line.startswith("["):
            continue
        head, _, rest = line.partition(" | ")
        idx = int(head[1:3])
        status = head[5:]
        fields = dict(part.split("=", 1) for part in rest.split(" | ") if "=" in part)
        if "帧数" in fields:
            fields["帧数"] = fields["帧数"].split("，")[0]
        recs.append({"idx": idx, "status": status, **fields})
    return recs


def wav_files(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return [n for n in os.listdir(directory) if n.lower().endswith(".wav")]


def main() -> int:
    print("== 步骤 0：确定性生成样本 ==")
    gen = subprocess.run(
        [sys.executable, "-I",
         os.path.join(os.path.dirname(os.path.abspath(__file__)), "make_samples.py"),
         ROOT],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    check("样本生成器退出码为 0", gen.returncode == 0, gen.stderr)

    out_main = os.path.join(ROOT, "out_main")
    out_bad = os.path.join(ROOT, "out_all_bad")
    out_u8 = os.path.join(ROOT, "out_u8")
    out_s24 = os.path.join(ROOT, "out_s24")
    out_empty = os.path.join(ROOT, "out_empty")
    out_broken = os.path.join(ROOT, "out_broken")
    out_noseg = os.path.join(ROOT, "out_noseg")
    out_stale = os.path.join(ROOT, "out_stale")

    print("\n== 验收 1/6：综合清单正常运行，产出音频与报告 ==")
    r = run_joiner("manifest_main.json", out_main)
    check("合法清单退出码为 0", r.returncode == 0, r.stderr)
    check("输出目录存在 joined.wav", os.path.isfile(os.path.join(out_main, "joined.wav")))
    check("输出目录存在 report.txt", os.path.isfile(os.path.join(out_main, "report.txt")))

    print("\n== 验收 2：参数不一致片段逐条跳过，其余照常拼接 ==")
    recs = parse_records(out_main)
    by_file = {os.path.basename(rec["文件"]): rec for rec in recs}
    check("d(采样率不同)被跳过且原因含采样率",
          by_file["d_int32_22050_mono.wav"]["status"] == "跳过"
          and "采样率" in by_file["d_int32_22050_mono.wav"]["原因"])
    check("e(声道不同)被跳过且原因含声道数",
          by_file["e_int32_16k_stereo.wav"]["status"] == "跳过"
          and "声道" in by_file["e_int32_16k_stereo.wav"]["原因"])
    check("f(位深不同)被跳过且原因含位深",
          by_file["f_int16_16k_mono.wav"]["status"] == "跳过"
          and "位深" in by_file["f_int16_16k_mono.wav"]["原因"])
    check("成功记录共 4 条，跳过记录共 9 条",
          sum(x["status"] == "成功" for x in recs) == 4
          and sum(x["status"] == "跳过" for x in recs) == 9)

    print("\n== 验收 3：声明长度错误的损坏片段被标记且数据不入结果 ==")
    check("g 被跳过且报告标记为损坏",
          by_file["g_bad_declared_length.wav"]["status"] == "跳过"
          and "损坏" in by_file["g_bad_declared_length.wav"]["原因"]
          and ("不符" in by_file["g_bad_declared_length.wav"]["原因"]
               or "不足" in by_file["g_bad_declared_length.wav"]["原因"]))
    check("h(截断)被跳过且报告标记为损坏",
          by_file["h_truncated.wav"]["status"] == "跳过"
          and "损坏" in by_file["h_truncated.wav"]["原因"])
    try:
        wav_joiner.inspect_wav(os.path.join(MAT, "g_bad_declared_length.wav"))
        inspect_ok = False
    except wav_joiner.WavError:
        inspect_ok = True
    check("inspect_wav 直接判定 g 损坏并抛 WavError", inspect_ok)

    print("\n== 验收 4：结果为有效 PCM，帧数为成功片段之和，边界无间隙 ==")
    out = read_wav(os.path.join(out_main, "joined.wav"))
    a = read_wav(os.path.join(MAT, "a_int32_16k_mono.wav"))
    b = read_wav(os.path.join(MAT, "b_int32_16k_mono.wav"))
    c = read_wav(os.path.join(MAT, "c_float32_16k_mono.wav"))
    check("输出编码为 PCM(0x0001)", out["tag"] == 0x0001)
    check("输出参数与首个成功片段一致(16kHz/单声道/32位)",
          out["rate"] == 16000 and out["channels"] == 1 and out["bits"] == 32)
    check("总采样帧数 = 1000+1000+1000+1000 = 4000", out["frames"] == 4000)
    expected = (a["data"] + b["data"]
                + independent_float_to_int32(c["data"]) + a["data"])
    check("输出字节流恰为 a+b+转换(c)+a，边界直接相连无静音/间隙",
          out["data"] == expected)
    with wave.open(os.path.join(out_main, "joined.wav"), "rb") as wf:
        check("标准库 wave 可打开结果文件且 nframes=4000（有效性交叉验证）",
              wf.getcomptype() == "NONE" and wf.getnframes() == 4000
              and wf.getframerate() == 16000 and wf.getnchannels() == 1
              and wf.getsampwidth() == 4)

    print("\n== 验收 5：浮点片段限幅转整数，报告注明转换，样本不越界 ==")
    check("c 的报告记录注明浮点->整数转换",
          by_file["c_float32_16k_mono.wav"]["status"] == "成功"
          and "转换" in by_file["c_float32_16k_mono.wav"].get("说明", ""))
    samples = list(struct.unpack("<%di" % (len(out["data"]) // 4), out["data"]))
    check("全部整数样本处于 int32 合法区间",
          all(INT32_MIN <= s <= INT32_MAX for s in samples))
    # c 从输出的第 2000 帧开始；前 4 个锚点 1.0/-1.0/1.5/-1.5 必须限幅
    anchor = samples[2000:2004]
    check("浮点锚点限幅正确 [MAX,MIN,MAX,MIN]",
          anchor == [INT32_MAX, INT32_MIN, INT32_MAX, INT32_MIN],
          f"实际={anchor}")

    print("\n== 验收 6：报告逐片段给出文件名/结论/原因且行序与清单一致 ==")
    with open(os.path.join(ROOT, "manifest_main.json"), "r", encoding="utf-8") as f:
        manifest_order = [os.path.basename(p) for p in json.load(f)["segments"]]
    report_order = [os.path.basename(rec["文件"]) for rec in recs]
    check("报告明细行序与清单完全一致（13 条）",
          report_order == manifest_order and len(recs) == 13)
    check("每条跳过记录都带原因",
          all(rec.get("原因", "") for rec in recs if rec["status"] == "跳过"))
    check("每条成功记录都带帧数",
          all(int(rec.get("帧数", "0")) > 0 for rec in recs if rec["status"] == "成功"))

    print("\n== 验收 7：全部不可用时不生成 WAV，但报告逐条说明 ==")
    r_bad = run_joiner("manifest_all_bad.json", out_bad)
    check("全损清单退出码仍为 0", r_bad.returncode == 0, r_bad.stderr)
    check("输出目录无任何 WAV 文件", wav_files(out_bad) == [])
    check("报告仍然生成", os.path.isfile(os.path.join(out_bad, "report.txt")))
    bad_recs = parse_records(out_bad)
    check("报告列出全部 6 个片段且均为跳过、均有原因",
          len(bad_recs) == 6
          and all(x["status"] == "跳过" and x.get("原因") for x in bad_recs))

    print("\n== 验收 8：缺失路径与文本文件跳过，其他片段继续成功 ==")
    check("缺失路径 m_missing.wav 被跳过且原因含路径不存在",
          by_file["m_missing.wav"]["status"] == "跳过"
          and "不存在" in by_file["m_missing.wav"]["原因"])
    check("文本文件 k_note.txt 被跳过且原因含扩展名",
          by_file["k_note.txt"]["status"] == "跳过"
          and "扩展名" in by_file["k_note.txt"]["原因"])
    check("假 WAV(i_fake) 被跳过且原因说明无法解析",
          by_file["i_fake.wav"]["status"] == "跳过"
          and "WAV" in by_file["i_fake.wav"]["原因"])
    check("非 PCM(j_ulaw) 被跳过且原因说明非 PCM 编码",
          by_file["j_ulaw.wav"]["status"] == "跳过"
          and "PCM" in by_file["j_ulaw.wav"]["原因"])

    print("\n== 验收 9：重复条目分别拼接、分别记录，不去重 ==")
    a_recs = [x for x in recs if os.path.basename(x["文件"]) == "a_int32_16k_mono.wav"]
    check("报告中 a 出现两条成功记录（清单第 1、13 位）",
          len(a_recs) == 2 and [x["idx"] for x in a_recs] == [1, 13]
          and all(x["status"] == "成功" for x in a_recs))
    check("输出帧数包含 a 的两份贡献（总帧数 4000 = 含重复的成功帧之和）",
          out["frames"] == sum(int(x["帧数"]) for x in recs if x["status"] == "成功"))

    print("\n== 验收 10：清单 JSON 损坏/缺字段时非 0 退出且无音频 ==")
    r_broken = run_joiner("manifest_broken_json.json", out_broken)
    check("损坏 JSON 返回非 0", r_broken.returncode != 0)
    check("损坏 JSON 给出可读错误信息", "JSON" in r_broken.stderr)
    check("损坏 JSON 运行后无任何 WAV 产出", wav_files(out_broken) == [])
    r_noseg = run_joiner("manifest_no_segments.json", out_noseg)
    check("缺少 segments 字段返回非 0", r_noseg.returncode != 0)
    check("缺字段错误信息可读", "segments" in r_noseg.stderr)
    check("缺字段运行后无任何 WAV 产出", wav_files(out_noseg) == [])

    print("\n== 附加验收：8 位 / 24 位同构拼接与空清单 ==")
    r_u8 = run_joiner("manifest_u8.json", out_u8)
    check("8 位清单退出码 0", r_u8.returncode == 0, r_u8.stderr)
    u8 = read_wav(os.path.join(out_u8, "joined.wav"))
    check("8 位输出参数 8kHz/单声道/8 位 PCM，帧数 500+600=1100",
          u8["tag"] == 0x0001 and u8["rate"] == 8000 and u8["channels"] == 1
          and u8["bits"] == 8 and u8["frames"] == 1100)
    r_s24 = run_joiner("manifest_s24.json", out_s24)
    check("24 位清单退出码 0", r_s24.returncode == 0, r_s24.stderr)
    s24 = read_wav(os.path.join(out_s24, "joined.wav"))
    check("24 位输出参数 48kHz/立体声/24 位 PCM，帧数 400+350=750",
          s24["tag"] == 0x0001 and s24["rate"] == 48000 and s24["channels"] == 2
          and s24["bits"] == 24 and s24["frames"] == 750)
    r_empty = run_joiner("manifest_empty.json", out_empty)
    check("空清单退出码 0", r_empty.returncode == 0, r_empty.stderr)
    check("空清单不产出 WAV 但生成报告",
          wav_files(out_empty) == []
          and os.path.isfile(os.path.join(out_empty, "report.txt")))

    print("\n== 附加验收：全损运行会清理同名陈旧 WAV ==")
    os.makedirs(out_stale, exist_ok=True)
    stale_wav = os.path.join(out_stale, "joined.wav")
    with open(stale_wav, "wb") as f:
        f.write(b"stale")
    run_joiner("manifest_all_bad.json", out_stale)
    check("陈旧 joined.wav 在全损运行后被删除", not os.path.exists(stale_wav))

    print("\n" + "=" * 60)
    print(f"验收汇总: 通过 {_passed} 项, 失败 {_failed} 项")
    if _failed:
        print("失败明细:")
        for item in _failures:
            print("  - " + item)
        return 1
    print("全部验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
