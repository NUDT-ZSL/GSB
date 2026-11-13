#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
podmaster —— 播客母带合成工具

按 JSON 剪辑清单将若干单声道 16 位 PCM WAV 片段合成为母带：
  * 每段独立分贝增益，增益后样本钳制在 16 位范围；
  * 相邻段支持线性交叉淡化（fade_ms 作用于本段与下一段之间）；
  * 母带首尾施加短淡入/淡出，避免爆音；
  * 清单全部校验通过才产出母带，非法项逐条报告；
  * 母带 master.wav 与报告 report.json 写入同一输出目录，原子覆盖。

仅使用 Python 标准库（3.10+）。

清单格式（version 1）：
{
  "version": 1,
  "segments": {"host": "host.wav", "guest": "guest.wav"},   // 路径相对清单所在目录
  "fade_in_ms": 20,        // 可选，母带首部淡入，默认 20
  "fade_out_ms": 50,       // 可选，母带尾部淡出，默认 50
  "clips": [
    {"segment": "host", "start_ms": 0, "end_ms": 1000,
     "gain_db": 0.0, "fade_ms": 100},   // fade_ms 为与下一段的交叉淡化，可省（默认 0）
    ...
  ]
}
"""
import argparse
import json
import os
import struct
import sys
import wave

SUPPORTED_VERSION = 1
DEFAULT_FADE_IN_MS = 20.0
DEFAULT_FADE_OUT_MS = 50.0
INT16_MIN, INT16_MAX = -32768, 32767
MASTER_NAME = "master.wav"
REPORT_NAME = "report.json"


class MasterError(Exception):
    """可预期的处理失败，信息面向用户。"""


def clamp16(v):
    if v < INT16_MIN:
        return INT16_MIN
    if v > INT16_MAX:
        return INT16_MAX
    return v


# ---------------------------------------------------------------- WAV 读取

def load_wav(path):
    """读取单声道 16 位 PCM WAV，返回 (采样率, 样本列表)。失败抛 MasterError。"""
    try:
        w = wave.open(path, "rb")
    except FileNotFoundError:
        raise MasterError(f"WAV 文件不存在: {path}")
    except (wave.Error, EOFError) as e:
        raise MasterError(f"无法解析 WAV 文件 {path}: {e}")
    with w:
        ch = w.getnchannels()
        if ch != 1:
            raise MasterError(f"文件 {path}: 非单声道（声道数={ch}），仅支持单声道")
        sw = w.getsampwidth()
        if sw != 2:
            raise MasterError(f"文件 {path}: 位深非 16 位（实际 {sw * 8} 位）")
        rate = w.getframerate()
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    got = len(raw) // 2
    if got < nframes:
        raise MasterError(
            f"文件 {path}: 数据截断（头部声明 {nframes} 帧，实际仅 {got} 帧）")
    samples = list(struct.unpack("<%dh" % got, raw[: got * 2]))
    return rate, samples


# ---------------------------------------------------------------- 清单读取与校验

def load_manifest(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise MasterError(f"无法读取清单 {path}: {e}")
    try:
        m = json.loads(text)
    except json.JSONDecodeError as e:
        raise MasterError(
            f"清单 {path} 不是合法 JSON: 第 {e.lineno} 行第 {e.colno} 列: {e.msg}")
    if not isinstance(m, dict):
        raise MasterError(f"清单 {path} 必须是 JSON 对象")
    for field in ("version", "segments", "clips"):
        if field not in m:
            raise MasterError(f"清单缺少必填字段: {field}")
    if m["version"] != SUPPORTED_VERSION:
        raise MasterError(
            f"不支持的清单版本 {m['version']!r}，本工具支持的版本: {SUPPORTED_VERSION}")
    if not isinstance(m["segments"], dict) or not m["segments"]:
        raise MasterError("清单字段 segments 必须是非空对象（名称 -> WAV 路径）")
    if not isinstance(m["clips"], list) or not m["clips"]:
        raise MasterError("清单字段 clips 必须是非空数组")
    return m


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_clips(manifest, seg_durations_ms):
    """逐条校验剪辑项，返回错误字符串列表（空列表表示全部合法）。

    seg_durations_ms: {片段名: 时长ms}
    """
    errors = []
    clips = manifest["clips"]
    durations = []  # 每段时长 ms，非法时为 None
    for i, c in enumerate(clips):
        tag = f"clips[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{tag}: 必须是对象")
            durations.append(None)
            continue
        ok = True
        seg = c.get("segment")
        if not isinstance(seg, str) or seg not in seg_durations_ms:
            errors.append(f"{tag}: 引用的片段编号不存在: {seg!r}")
            ok = False
        start, end = c.get("start_ms"), c.get("end_ms")
        if not _num(start) or not _num(end):
            errors.append(f"{tag}: start_ms/end_ms 必须是数字")
            ok = False
        else:
            if start < 0:
                errors.append(f"{tag}: start_ms 为负（{start}）")
                ok = False
            if end <= start:
                errors.append(f"{tag}: end_ms（{end}）必须大于 start_ms（{start}）")
                ok = False
            elif ok and seg in seg_durations_ms and end > seg_durations_ms[seg] + 1e-9:
                errors.append(
                    f"{tag}: end_ms（{end}）越界，片段 {seg!r} 时长仅 "
                    f"{seg_durations_ms[seg]:.1f}ms")
                ok = False
        if "gain_db" in c and not _num(c["gain_db"]):
            errors.append(f"{tag}: gain_db 必须是数字")
            ok = False
        fade = c.get("fade_ms", 0)
        if not _num(fade) or fade < 0:
            errors.append(f"{tag}: fade_ms 必须是非负数字")
            ok = False
        durations.append((end - start) if ok else None)

    # 交叉淡化不得超过较短相邻片段时长
    for i, c in enumerate(clips):
        fade = c.get("fade_ms", 0) if isinstance(c, dict) else 0
        if not _num(fade) or fade <= 0:
            continue
        if i + 1 >= len(clips):
            errors.append(f"clips[{i}]: 最后一段不能设置交叉淡化（fade_ms={fade}）")
            continue
        d_cur, d_nxt = durations[i], durations[i + 1]
        if d_cur is None or d_nxt is None:
            continue  # 相邻段自身已报错，不重复报告
        shorter = min(d_cur, d_nxt)
        if fade > shorter + 1e-9:
            errors.append(
                f"clips[{i}]: 交叉淡化 {fade}ms 超过较短相邻片段时长 "
                f"{shorter:.1f}ms，非法")
    return errors


# ---------------------------------------------------------------- 合成

def gain_db_to_factor(db):
    return 10.0 ** (db / 20.0)


def process(manifest, seg_data, rate):
    """合成母带，返回样本列表。seg_data: {片段名: (rate, samples)}"""
    clips = manifest["clips"]
    pieces = []
    for c in clips:
        _, samples = seg_data[c["segment"]]
        s = round(c["start_ms"] * rate / 1000.0)
        e = round(c["end_ms"] * rate / 1000.0)
        g = gain_db_to_factor(c.get("gain_db", 0.0))
        pieces.append([clamp16(round(x * g)) for x in samples[s:e]])

    # 顺序拼接 + 相邻线性交叉淡化
    out = pieces[0][:]
    for i in range(1, len(pieces)):
        fade = round(clips[i - 1].get("fade_ms", 0) * rate / 1000.0)
        nxt = pieces[i]
        if fade > 0:
            tail, head = out[-fade:], nxt[:fade]
            mixed = [clamp16(round(tail[k] * (fade - k) / fade + head[k] * k / fade))
                     for k in range(fade)]
            out = out[:-fade] + mixed + nxt[fade:]
        else:
            out = out + nxt

    # 母带首尾短淡入/淡出
    fi = round(manifest.get("fade_in_ms", DEFAULT_FADE_IN_MS) * rate / 1000.0)
    fo = round(manifest.get("fade_out_ms", DEFAULT_FADE_OUT_MS) * rate / 1000.0)
    n = len(out)
    fi = min(fi, n)
    fo = min(fo, n - fi)
    for k in range(fi):
        out[k] = clamp16(round(out[k] * k / fi)) if fi else out[k]
    for k in range(fo):
        out[n - 1 - k] = clamp16(round(out[n - 1 - k] * k / fo)) if fo else out[n - 1 - k]
    return out


def write_wav(path, rate, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))


def atomic_write(path, write_fn):
    """先写临时文件再替换，失败不留半成品。"""
    tmp = path + ".tmp"
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 主流程

def run(manifest_path, outdir):
    manifest = load_manifest(manifest_path)
    base = os.path.dirname(os.path.abspath(manifest_path))

    # 加载片段 WAV（逐文件收集错误）
    errors = []
    seg_data = {}
    for name, rel in manifest["segments"].items():
        if not isinstance(rel, str):
            errors.append(f"segments.{name}: 路径必须是字符串")
            continue
        path = rel if os.path.isabs(rel) else os.path.join(base, rel)
        try:
            seg_data[name] = load_wav(path)
        except MasterError as e:
            errors.append(str(e))
    if errors:
        raise MasterError("\n".join(errors))

    # 采样率一致性
    rates = {name: r for name, (r, _) in seg_data.items()}
    if len(set(rates.values())) > 1:
        detail = ", ".join(f"{n}={r}Hz" for n, r in rates.items())
        raise MasterError(f"采样率不一致，拒绝处理（{detail}）；所有片段采样率必须相同")
    rate = next(iter(rates.values()))

    seg_durations = {n: len(s) * 1000.0 / r for n, (r, s) in seg_data.items()}
    errors = validate_clips(manifest, seg_durations)
    if errors:
        raise MasterError("清单校验失败，共 %d 条非法项：\n%s"
                          % (len(errors), "\n".join("  - " + e for e in errors)))

    out = process(manifest, seg_data, rate)

    os.makedirs(outdir, exist_ok=True)
    master_path = os.path.join(outdir, MASTER_NAME)
    report_path = os.path.join(outdir, REPORT_NAME)
    atomic_write(master_path, lambda p: write_wav(p, rate, out))

    clips = manifest["clips"]
    report = {
        "version": SUPPORTED_VERSION,
        "sample_rate": rate,
        "segments": [
            {
                "index": i,
                "source": c["segment"],
                "file": manifest["segments"][c["segment"]],
                "start_ms": c["start_ms"],
                "end_ms": c["end_ms"],
                "duration_ms": round(c["end_ms"] - c["start_ms"], 3),
                "gain_db": c.get("gain_db", 0.0),
                "crossfade_to_next_ms": c.get("fade_ms", 0) if i + 1 < len(clips) else 0,
            }
            for i, c in enumerate(clips)
        ],
        "fade_in_ms": manifest.get("fade_in_ms", DEFAULT_FADE_IN_MS),
        "fade_out_ms": manifest.get("fade_out_ms", DEFAULT_FADE_OUT_MS),
        "total_samples": len(out),
        "total_duration_ms": round(len(out) * 1000.0 / rate, 3),
        "master_file": MASTER_NAME,
    }
    atomic_write(report_path, lambda p: _write_json(p, report))

    print(f"母带: {master_path}")
    print(f"报告: {report_path}")
    print(f"总时长: {report['total_duration_ms']}ms（{len(out)} 样本 @ {rate}Hz）")
    return 0


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="podmaster", description="按 JSON 剪辑清单合成播客母带（单声道 16 位 PCM WAV）")
    ap.add_argument("manifest", help="剪辑清单 JSON 路径")
    ap.add_argument("-o", "--outdir", required=True, help="输出目录（母带与报告）")
    args = ap.parse_args(argv)
    try:
        return run(args.manifest, args.outdir)
    except MasterError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
