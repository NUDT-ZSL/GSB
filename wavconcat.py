#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wavconcat — 按 JSON 清单顺序拼接 PCM16 单声道 WAV 片段，并在相邻接缝处做短线性淡变。

用法:
    python wavconcat.py <manifest.json> <output.wav>

清单格式 (UTF-8 JSON):
    {"files": [{"path": "a.wav", "fade_ms": 20}, {"path": "b.wav"}, ...]}

规则:
  * 仅接受 16 位、单声道、采样率 8000..48000 Hz 的 WAV；其余格式报错退出。
  * 所有片段采样率必须与首片段一致，否则整体拒绝且不写输出。
  * fade_ms 为 0..500 的整数；第 i 项的 fade_ms 作用于第 i 项与第 i+1 项之间的接缝
    （前段尾部淡出 + 后段头部淡入，帧数相同）。首片段头部与末片段尾部不做淡变。
  * 每片段两端淡变帧数之和不得超过该片段帧数的一半。
  * 淡变为线性整数增益：淡出第 k 帧增益 (N-k)/N，淡入第 k 帧增益 k/N（N 为淡变帧数）。
  * 样本换算全部整数运算并饱和裁剪到 [-32768, 32767]。
  * 片段路径相对清单所在目录解析，解析结果越出该目录即拒绝。
  * 输出先写同目录临时文件，再原子替换目标；失败时旧输出保持不变。
"""
import argparse
import json
import os
import struct
import sys
import tempfile
import wave

MIN_RATE = 8000
MAX_RATE = 48000
MAX_FADE_MS = 500
SAMPLE_MIN = -32768
SAMPLE_MAX = 32767


class ConcatError(Exception):
    """可预期的校验/处理错误，消息打印到 stderr 并以非零码退出。"""


def load_manifest(manifest_path):
    """读取并校验清单，返回 [(path, fade_ms), ...]（顺序即拼接顺序）。"""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConcatError("清单文件不存在: %s" % manifest_path)
    except UnicodeDecodeError as e:
        raise ConcatError("清单不是合法 UTF-8: %s (%s)" % (manifest_path, e))
    except json.JSONDecodeError as e:
        raise ConcatError("清单不是合法 JSON: %s (%s)" % (manifest_path, e))

    if not isinstance(data, dict) or "files" not in data:
        raise ConcatError("清单必须是含 files 数组的 JSON 对象")
    files = data["files"]
    if not isinstance(files, list):
        raise ConcatError("清单 files 字段必须是数组")
    if len(files) == 0:
        raise ConcatError("清单 files 数组为空，拒绝输出 0 帧 WAV")

    entries = []
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            raise ConcatError("files[%d] 必须是对象" % i)
        if "path" not in item:
            raise ConcatError("files[%d] 缺少 path 字段" % i)
        path = item["path"]
        if not isinstance(path, str) or not path:
            raise ConcatError("files[%d] 的 path 必须是非空字符串" % i)
        fade = item.get("fade_ms", 0)
        if isinstance(fade, bool) or not isinstance(fade, int):
            raise ConcatError(
                "files[%d] 的 fade_ms 必须是整数，得到 %r" % (i, fade))
        if not (0 <= fade <= MAX_FADE_MS):
            raise ConcatError(
                "files[%d] 的 fade_ms=%d 越界，允许范围 0..%d"
                % (i, fade, MAX_FADE_MS))
        entries.append((path, fade))
    return entries


def resolve_clip_path(manifest_dir, rel_path, index):
    """相对清单目录解析片段路径；越出该目录即拒绝。"""
    base = os.path.normcase(os.path.abspath(manifest_dir))
    resolved = os.path.normcase(os.path.abspath(os.path.join(base, rel_path)))
    if os.path.commonpath([base, resolved]) != base:
        raise ConcatError(
            "files[%d] 路径越界: %r 解析后越出清单目录 %s"
            % (index, rel_path, base))
    return resolved


def read_clip(path, index):
    """读取并校验一个 WAV 片段，返回 (采样率, [样本...])。"""
    try:
        w = wave.open(path, "rb")
    except FileNotFoundError:
        raise ConcatError("files[%d] 片段不存在: %s" % (index, path))
    except (wave.Error, EOFError) as e:
        raise ConcatError(
            "files[%d] 无法解析为合法 WAV: %s (%s)" % (index, path, e))
    with w:
        if w.getcomptype() != "NONE":
            raise ConcatError(
                "files[%d] 格式不支持: 压缩类型 %s，仅支持未压缩 PCM"
                % (index, w.getcomptype()))
        if w.getnchannels() != 1:
            raise ConcatError(
                "files[%d] 格式不支持: %d 声道，仅支持单声道"
                % (index, w.getnchannels()))
        if w.getsampwidth() != 2:
            raise ConcatError(
                "files[%d] 格式不支持: %d 位样本，仅支持 16 位 PCM"
                % (index, w.getsampwidth() * 8))
        rate = w.getframerate()
        if not (MIN_RATE <= rate <= MAX_RATE):
            raise ConcatError(
                "files[%d] 格式不支持: 采样率 %d Hz，允许范围 %d..%d Hz"
                % (index, rate, MIN_RATE, MAX_RATE))
        nframes = w.getnframes()
        raw = w.readframes(nframes)
    if len(raw) != nframes * 2:
        raise ConcatError(
            "files[%d] WAV 数据长度与帧头不符: %s" % (index, path))
    samples = list(struct.unpack("<%dh" % nframes, raw))
    return rate, samples


def clamp16(v):
    if v > SAMPLE_MAX:
        return SAMPLE_MAX
    if v < SAMPLE_MIN:
        return SAMPLE_MIN
    return v


def scale_sample(s, num, den):
    """整数线性增益 s*num/den，向零取整（正负对称），并饱和裁剪到 16 位。"""
    if s >= 0:
        v = (s * num) // den
    else:
        v = -((-s * num) // den)
    return clamp16(v)


def apply_fades(clips, fade_frames):
    """就地（在拷贝上）施加接缝淡变。fade_frames[i] 为第 i 条接缝的淡变帧数。

    接缝 i 位于片段 i 与片段 i+1 之间：片段 i 尾部淡出，片段 i+1 头部淡入。
    """
    n = len(clips)
    for seam in range(n - 1):
        nf = fade_frames[seam]
        if nf <= 0:
            continue
        tail = clips[seam]       # 前段尾部淡出: 第 k 帧增益 (nf-k)/nf
        base = len(tail) - nf
        for k in range(nf):
            tail[base + k] = scale_sample(tail[base + k], nf - k, nf)
        head = clips[seam + 1]   # 后段头部淡入: 第 k 帧增益 k/nf
        for k in range(nf):
            head[k] = scale_sample(head[k], k, nf)


def write_wav_atomic(out_path, rate, samples):
    """先写输出同目录临时文件，再原子替换目标；任何失败都不影响旧输出。"""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".wavconcat-", suffix=".tmp",
                                    dir=out_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            with wave.open(f, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(struct.pack("<%dh" % len(samples), *samples))
        os.replace(tmp_path, out_path)  # 同目录原子替换
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def concat(manifest_path, out_path):
    manifest_path = os.path.abspath(manifest_path)
    manifest_dir = os.path.dirname(manifest_path)

    entries = load_manifest(manifest_path)

    # 先完整读入并校验所有片段，任何失败都发生在写输出之前。
    clips = []
    rates = []
    for i, (rel, _fade) in enumerate(entries):
        path = resolve_clip_path(manifest_dir, rel, i)
        rate, samples = read_clip(path, i)
        if len(samples) == 0:
            raise ConcatError(
                "files[%d] 片段为 0 帧: %s，拒绝输出 0 帧 WAV" % (i, path))
        rates.append(rate)
        clips.append(samples)

    base_rate = rates[0]
    for i in range(1, len(rates)):
        if rates[i] != base_rate:
            raise ConcatError(
                "采样率不一致: files[0] 为 %d Hz，files[%d] 为 %d Hz，整体拒绝"
                % (base_rate, i, rates[i]))

    # 第 i 条接缝（片段 i 与 i+1 之间）的淡变帧数由 files[i].fade_ms 决定。
    fade_frames = [(entries[i][1] * base_rate) // 1000
                   for i in range(len(entries) - 1)]

    # 每片段两端淡变帧数之和不得超过该片段帧数的一半。
    for i, samples in enumerate(clips):
        head = fade_frames[i - 1] if i > 0 else 0
        tail = fade_frames[i] if i < len(fade_frames) else 0
        if 2 * (head + tail) > len(samples):
            raise ConcatError(
                "files[%d] 帧数不足: 片段共 %d 帧，两端淡变合计 %d 帧"
                "（头部 %d + 尾部 %d），超过帧数一半"
                % (i, len(samples), head + tail, head, tail))

    apply_fades(clips, fade_frames)

    total_frames = sum(len(c) for c in clips)
    merged = [s for c in clips for s in c]
    write_wav_atomic(out_path, base_rate, merged)

    duration = total_frames / base_rate
    print("片段数: %d" % len(clips))
    print("采样率: %d Hz" % base_rate)
    print("总帧数: %d" % total_frames)
    print("时长: %.3f s" % duration)
    print("输出: %s" % os.path.abspath(out_path))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="按 JSON 清单拼接 PCM16 单声道 WAV，接缝处做短线性淡变")
    parser.add_argument("manifest", help="UTF-8 JSON 清单路径")
    parser.add_argument("output", help="输出 WAV 路径")
    args = parser.parse_args(argv)
    try:
        concat(args.manifest, args.output)
    except ConcatError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("错误: 文件读写失败: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
