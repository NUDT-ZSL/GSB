# -*- coding: utf-8 -*-
"""wavedit.py — 本地 PCM WAV 时间片段变速变调工具（仅标准库）。

用法:
    python wavedit.py <源WAV> <配置JSON> <输出WAV>

退出码:
    0  成功
    1  源文件缺失 / 无法解析 / 不支持的WAV格式
    2  配置错误（缺字段、字段越界、JSON无法解析等）
    3  输出路径不可写
"""
import json
import math
import os
import struct
import sys

# ---------------------------------------------------------------- 常量
MIN_RATE, MAX_RATE = 8000, 48000
ALLOWED_BITS = (8, 16, 24, 32)
ALLOWED_CHANNELS = (1, 2)
MAX_PITCH_SEMITONES = 48.0
REQUIRED_FIELDS = ("start", "end", "speed", "pitch")


class UnsupportedWav(Exception):
    """不支持的WAV格式（满足约束但非本工具可处理的PCM）。"""


class UnparseableWav(Exception):
    """文件无法按RIFF/WAVE解析。"""


# ---------------------------------------------------------------- WAV 解析
def parse_wav(raw):
    """解析WAV字节串，返回 (channels, samplerate, bits, data_bytes)。"""
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise UnparseableWav("不是合法的RIFF/WAVE文件")
    fmt = None
    data = None
    pos = 12
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if len(body) < size:
            raise UnparseableWav("数据块 %r 长度不足，文件可能已损坏" % cid)
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or len(fmt) < 16:
        raise UnparseableWav("缺少fmt块")
    if data is None:
        raise UnparseableWav("缺少data块")

    audio_fmt, channels, rate, _byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", fmt[:16])
    if audio_fmt != 1:
        raise UnsupportedWav(
            "不支持的WAV格式：仅支持PCM(format=1)，实际format=%d" % audio_fmt)
    if not (MIN_RATE <= rate <= MAX_RATE):
        raise UnsupportedWav(
            "不支持的WAV格式：采样率%dHz，需在%d~%dHz之间" % (rate, MIN_RATE, MAX_RATE))
    if bits not in ALLOWED_BITS:
        raise UnsupportedWav(
            "不支持的WAV格式：位深%dbit，仅支持8/16/24/32" % bits)
    if channels not in ALLOWED_CHANNELS:
        raise UnsupportedWav(
            "不支持的WAV格式：声道数%d，仅支持1或2" % channels)
    frame_bytes = channels * (bits // 8)
    if block_align != frame_bytes:
        raise UnparseableWav("块对齐字段与声道/位深不一致")
    if len(data) % frame_bytes != 0:
        raise UnparseableWav("data块长度不是采样帧的整数倍")
    return channels, rate, bits, data


def build_wav(channels, rate, bits, data):
    """按参数构造确定性WAV字节串（fmt+data两块）。"""
    block_align = channels * (bits // 8)
    byte_rate = rate * block_align
    fmt = struct.pack("<HHIIHH", 1, channels, rate, byte_rate, block_align, bits)
    pad = b"\x00" if len(data) & 1 else b""
    riff_size = 4 + (8 + len(fmt)) + (8 + len(data) + len(pad))
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", len(data)) + data + pad)


# ---------------------------------------------------------------- PCM 编解码
def decode_pcm(data, bits, channels):
    """解码为每声道浮点列表，范围[-1.0, 1.0)。8位无符号，其余有符号。"""
    chs = [[] for _ in range(channels)]
    if bits == 8:
        for i, b in enumerate(data):
            chs[i % channels].append((b - 128) / 128.0)
    elif bits == 16:
        vals = struct.unpack("<%dh" % (len(data) // 2), data)
        for i, v in enumerate(vals):
            chs[i % channels].append(v / 32768.0)
    elif bits == 24:
        n = len(data) // 3
        for i in range(n):
            o = i * 3
            v = data[o] | (data[o + 1] << 8) | (data[o + 2] << 16)
            if v >= 0x800000:
                v -= 0x1000000
            chs[i % channels].append(v / 8388608.0)
    else:  # 32
        vals = struct.unpack("<%di" % (len(data) // 4), data)
        for i, v in enumerate(vals):
            chs[i % channels].append(v / 2147483648.0)
    return chs


def _quantize(v, bits):
    """浮点->整型采样，四舍五入并饱和到位深合法范围。"""
    scale = float(1 << (bits - 1))
    q = int(math.floor(v * scale + 0.5))
    lo, hi = -int(scale), int(scale) - 1
    if q < lo:
        return lo
    if q > hi:
        return hi
    return q


def encode_pcm(chs, bits):
    """浮点声道列表编码回PCM字节串。"""
    channels = len(chs)
    frames = len(chs[0]) if chs else 0
    out = bytearray()
    if bits == 8:
        for i in range(frames):
            for c in range(channels):
                out.append(_quantize(chs[c][i], 8) + 128)
    elif bits == 16:
        pack = out.extend
        for i in range(frames):
            for c in range(channels):
                pack(struct.pack("<h", _quantize(chs[c][i], 16)))
    elif bits == 24:
        for i in range(frames):
            for c in range(channels):
                q = _quantize(chs[c][i], 24) & 0xFFFFFF
                out.append(q & 0xFF)
                out.append((q >> 8) & 0xFF)
                out.append((q >> 16) & 0xFF)
    else:  # 32
        for i in range(frames):
            for c in range(channels):
                out.extend(struct.pack("<i", _quantize(chs[c][i], 32)))
    return bytes(out)


# ---------------------------------------------------------------- DSP
def resample_channel(x, out_len):
    """线性插值重采样到指定长度。"""
    n = len(x)
    if out_len <= 0:
        return []
    if n == 0:
        return [0.0] * out_len
    if n == 1 or out_len == 1:
        return [x[0]] * out_len
    step = (n - 1) / (out_len - 1)
    y = []
    last = n - 1
    for i in range(out_len):
        pos = i * step
        i0 = int(pos)
        if i0 >= last:
            y.append(x[last])
        else:
            f = pos - i0
            y.append(x[i0] * (1.0 - f) + x[i0 + 1] * f)
    return y


def time_stretch_channel(x, out_len):
    """重叠相加(OLA)时长调整：改变样本数而不改变音调。"""
    n = len(x)
    if n == 0:
        return [0.0] * out_len
    if out_len <= 0:
        return []
    if n < 64 or out_len < 64:
        return resample_channel(x, out_len)
    win = 512 if n >= 512 else (n // 4 * 4 or 4)
    hop = win // 4
    ratio = n / float(out_len)  # 每个输出样本对应的输入样本数
    # Hann窗
    w = [0.5 - 0.5 * math.cos(2.0 * math.pi * k / (win - 1)) for k in range(win)]
    out = [0.0] * out_len
    wsum = [0.0] * out_len
    pos_in = 0.0
    pos_out = 0
    while pos_out < out_len:
        base = int(pos_in)
        frac = pos_in - base
        for k in range(win):
            o = pos_out + k
            if o >= out_len:
                break
            idx = base + k
            if idx >= n - 1:
                v = x[n - 1] if n else 0.0
            else:
                v = x[idx] * (1.0 - frac) + x[idx + 1] * frac
            wk = w[k]
            out[o] += wk * v
            wsum[o] += wk
        pos_in += hop * ratio
        pos_out += hop
    for i in range(out_len):
        s = wsum[i]
        out[i] = out[i] / s if s > 1e-8 else 0.0
    return out


def pitch_shift_channel(x, semitones):
    """不变速音调调整：先按比率重采样（变调变速），再OLA拉伸回原时长。"""
    n = len(x)
    if n == 0 or semitones == 0:
        return list(x)
    r = 2.0 ** (semitones / 12.0)
    shifted = resample_channel(x, max(1, int(math.floor(n / r + 0.5))))
    return time_stretch_channel(shifted, n)


def sec_to_samples(sec, rate):
    """秒 -> 毫秒 -> 样本位置，均四舍五入到整数。"""
    ms = int(math.floor(sec * 1000.0 + 0.5))
    return int(math.floor(ms * rate / 1000.0 + 0.5))


def apply_edit(chs, rate, edit):
    """对当前音频施加一条编辑：先不变速变调，再变速，拼回时间轴。"""
    frames = len(chs[0]) if chs else 0
    s = sec_to_samples(edit["start"], rate)
    e = sec_to_samples(edit["end"], rate)
    # 起止超出时长时截断到有效范围
    s = max(0, min(s, frames))
    e = max(0, min(e, frames))
    if e <= s:
        return chs  # 截断后为空，等价无操作
    speed = edit["speed"]
    pitch = edit["pitch"]
    out_chs = []
    for x in chs:
        seg = x[s:e]
        if pitch != 0:
            seg = pitch_shift_channel(seg, pitch)
        # 变速：样本数按速度比例四舍五入取整
        new_len = int(math.floor(len(seg) / speed + 0.5))
        seg = resample_channel(seg, new_len)
        out_chs.append(x[:s] + seg + x[e:])
    return out_chs


# ---------------------------------------------------------------- 配置
def fail(msg, code):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(code)


def load_config(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        fail("配置错误：无法读取配置文件 %s（%s）" % (path, exc), 2)
    try:
        cfg = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        fail("配置错误：JSON解析失败（%s）" % exc, 2)
    if not isinstance(cfg, dict) or "edit" not in cfg:
        fail("配置错误：缺少字段 edit（应为编辑操作列表）", 2)
    edits = cfg["edit"]
    if not isinstance(edits, list):
        fail("配置错误：字段 edit 必须为列表", 2)
    for i, item in enumerate(edits):
        if not isinstance(item, dict):
            fail("配置错误：edit[%d] 必须为对象" % i, 2)
        for field in REQUIRED_FIELDS:
            if field not in item:
                fail("配置错误：edit[%d] 缺少字段 %s" % (i, field), 2)
            v = item[field]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                fail("配置错误：edit[%d] 字段 %s 必须为数字" % (i, field), 2)
            if not math.isfinite(v):
                fail("配置错误：edit[%d] 字段 %s 必须为有限数值" % (i, field), 2)
        if item["start"] < 0:
            fail("配置错误：edit[%d] 字段 start 越界（不能为负）" % i, 2)
        if item["end"] <= item["start"]:
            fail("配置错误：edit[%d] 字段 end 越界（必须大于start）" % i, 2)
        if item["speed"] <= 0:
            fail("配置错误：edit[%d] 字段 speed 越界（必须大于0）" % i, 2)
        if abs(item["pitch"]) > MAX_PITCH_SEMITONES:
            fail("配置错误：edit[%d] 字段 pitch 越界（|pitch|≤%g 半音）"
                 % (i, MAX_PITCH_SEMITONES), 2)
    return edits


# ---------------------------------------------------------------- 输出
def write_output(path, payload):
    """先写临时文件再原子替换；失败清理临时文件，避免残留损坏输出。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        fail("输出错误：无法写入输出路径 %s（%s）" % (path, exc), 3)


def print_summary(n_edits, frames, rate, out_bytes):
    dur = frames / float(rate)
    print("编辑操作：%d 个，总时长：%.1f 秒，输出字节：%d 字节"
          % (n_edits, dur, out_bytes))


# ---------------------------------------------------------------- 主流程
def main(argv):
    if len(argv) != 4:
        sys.stderr.write("用法: python wavedit.py <源WAV> <配置JSON> <输出WAV>\n")
        return 1
    src_path, cfg_path, out_path = argv[1], argv[2], argv[3]

    try:
        with open(src_path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        fail("源文件错误：无法读取 %s（%s）" % (src_path, exc), 1)

    try:
        channels, rate, bits, data = parse_wav(raw)
    except UnsupportedWav as exc:
        fail(str(exc), 1)
    except UnparseableWav as exc:
        fail("无法解析WAV文件：%s" % exc, 1)

    edits = load_config(cfg_path)

    if not edits:
        # 无编辑操作：原样复制
        write_output(out_path, raw)
        print("无编辑操作，已将源文件原样复制到输出路径。")
        frames = len(data) // (channels * (bits // 8))
        print_summary(0, frames, rate, len(raw))
        return 0

    try:
        chs = decode_pcm(data, bits, channels)
        for edit in edits:  # 顺序施加：后定义操作作用于前一结果
            chs = apply_edit(chs, rate, edit)
        out_data = encode_pcm(chs, bits)
        payload = build_wav(channels, rate, bits, out_data)
    except Exception as exc:  # 处理失败：不得残留输出，源文件不受影响
        fail("处理失败：%s: %s" % (type(exc).__name__, exc), 1)

    write_output(out_path, payload)
    frames = len(chs[0]) if chs else 0
    print_summary(len(edits), frames, rate, len(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
