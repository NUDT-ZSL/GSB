# -*- coding: utf-8 -*-
"""
make_samples.py — 确定性生成演示/验收所需的 WAV 样本与 JSON 清单。

仅使用标准库；不依赖随机数，同一环境重复运行产出字节级一致的素材：
  * 合法 PCM 整数 WAV：8 / 16 / 24 / 32 位，覆盖不同采样率与声道数
  * 合法 32 位 IEEE 浮点 WAV（含超范围采样用于验证限幅）
  * 损坏样本：声明 data 长度与实际不符、文件截断、假 WAV、非 PCM 编码
  * 非 WAV 文本文件、清单中引用但不生成的缺失路径
  * 多份 JSON 清单，覆盖任务全部验收场景

用法:
    python make_samples.py [输出根目录]   # 默认 ./demo
"""
from __future__ import annotations

import json
import math
import os
import shutil
import struct
import sys

# Windows 控制台默认可能是 GBK 代码页，统一强制 UTF-8，保证中文输出可读
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

PI2 = 2.0 * math.pi


# --------------------------------------------------------------------------- #
# RIFF 写入工具
# --------------------------------------------------------------------------- #
def write_riff_wav(
    path: str,
    format_tag: int,
    channels: int,
    sample_rate: int,
    bits_per_sample: int,
    data: bytes,
) -> None:
    """按给定编码写出结构完整的 RIFF/WAVE 文件。"""
    block_align = channels * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    fmt_body = struct.pack(
        "<HHIIHH",
        format_tag,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    riff_size = 4 + (8 + len(fmt_body)) + (8 + len(data))
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", riff_size))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", len(fmt_body)))
        f.write(fmt_body)
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


# --------------------------------------------------------------------------- #
# 确定性波形：每通道 440Hz 正弦，每通道叠加不同直流偏移以便逐样本核对
# --------------------------------------------------------------------------- #
def pcm_data(
    channels: int, frames: int, sample_rate: int, bits: int, frequency: float = 440.0
) -> bytes:
    signed = bits != 8  # WAV 约定：8 位 PCM 无符号，其余有符号
    if bits == 8:
        lo, hi = 0, 255
        zero = 128
        width = 127
    elif bits == 16:
        lo, hi = -32768, 32767
        zero = 0
        width = 32767
    elif bits == 24:
        lo, hi = -8_388_608, 8_388_607
        zero = 0
        width = 8_388_607
    else:  # 32 位整数
        lo, hi = -2_147_483_648, 2_147_483_647
        zero = 0
        width = 2_147_483_647

    out = bytearray()
    for n in range(frames):
        for ch in range(channels):
            offset = (ch + 1) * 0.1  # 每通道不同偏移，便于验证声道顺序
            v = 0.55 * math.sin(PI2 * frequency * n / sample_rate) + offset
            if signed:
                sample = int(zero + v * width)
            else:
                sample = int(zero + v * width)
            sample = max(lo, min(hi, sample))
            if bits == 8:
                out += struct.pack("<B", sample)
            elif bits == 16:
                out += struct.pack("<h", sample)
            elif bits == 24:
                out += struct.pack("<i", sample)[0:3]
            else:
                out += struct.pack("<i", sample)
    return bytes(out)


def float32_data(
    channels: int, frames: int, sample_rate: int, frequency: float = 330.0
) -> bytes:
    """32 位浮点数据；前 4 个采样刻意越界/含 ±1.0，供限幅验收。"""
    anchors = [1.0, -1.0, 1.5, -1.5]
    out = bytearray()
    for n in range(frames):
        for ch in range(channels):
            if n < len(anchors):
                x = anchors[n]
            else:
                x = (
                    0.6 * math.sin(PI2 * frequency * n / sample_rate)
                    + (ch + 1) * 0.05
                )
            out += struct.pack("<f", x)
    return bytes(out)


def write_manifest(path: str, segments: list, description: str) -> None:
    doc = {"description": description, "segments": segments}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# 损坏样本
# --------------------------------------------------------------------------- #
def make_bad_declared_length(src_path: str, dst_path: str) -> None:
    """复制合法 WAV 后把 data 块声明长度改大，真实字节数不变 -> 声明与实际不符。"""
    with open(src_path, "rb") as f:
        blob = bytearray(f.read())
    # 本项目生成的文件顺序固定为 RIFF(12) + fmt(8+16=24) + data 头(8)，
    # data 长度字段位于第 40 字节
    data_size_pos = 12 + 8 + 16 + 4
    declared = struct.unpack_from("<I", blob, data_size_pos)[0]
    struct.pack_into("<I", blob, data_size_pos, declared + 1000)
    with open(dst_path, "wb") as f:
        f.write(blob)


def make_truncated(src_path: str, dst_path: str) -> None:
    """直接截掉文件尾部，data 块真实字节少于声明。"""
    with open(src_path, "rb") as f:
        blob = f.read()
    with open(dst_path, "wb") as f:
        f.write(blob[: len(blob) - 200])


def make_fake_wav(path: str) -> None:
    """扩展名为 .wav，但内容是纯文本，无法解析为 RIFF/WAVE。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("this is not a wav file, just plain text renamed to .wav\n")


def make_non_pcm_wav(path: str) -> None:
    """结构合法但编码为 μ-law(0x0007)，非 PCM/浮点，应被拒绝。"""
    write_riff_wav(
        path,
        format_tag=0x0007,
        channels=1,
        sample_rate=8000,
        bits_per_sample=8,
        data=b"\x80" * 400,
    )


def main(root: str = "demo") -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    mat = os.path.join(root, "materials")
    os.makedirs(mat, exist_ok=True)

    def p(name: str, tag, ch, rate, bits, frames, freq=440.0):
        data = pcm_data(ch, frames, rate, bits, freq)
        write_riff_wav(os.path.join(mat, name), tag, ch, rate, bits, data)

    def flt(name, ch, rate, frames):
        data = float32_data(ch, frames, rate)
        write_riff_wav(
            os.path.join(mat, name), 0x0003, ch, rate, 32, data
        )

    # ---- 基准组：16kHz / 单声道 / 32 位整数，1000 帧/个 --------------------
    p("a_int32_16k_mono.wav", 0x0001, 1, 16000, 32, 1000, freq=392.0)
    p("b_int32_16k_mono.wav", 0x0001, 1, 16000, 32, 1000, freq=523.25)
    flt("c_float32_16k_mono.wav", 1, 16000, 1000)

    # ---- 参数不一致组（合法文件，但与基准不匹配，应逐条跳过） --------------
    p("d_int32_22050_mono.wav", 0x0001, 1, 22050, 32, 800)          # 采样率不同
    p("e_int32_16k_stereo.wav", 0x0001, 2, 16000, 32, 700)          # 声道不同
    p("f_int16_16k_mono.wav", 0x0001, 1, 16000, 16, 900)            # 位深不同

    # ---- 损坏组 -----------------------------------------------------------
    make_bad_declared_length(
        os.path.join(mat, "a_int32_16k_mono.wav"),
        os.path.join(mat, "g_bad_declared_length.wav"),
    )
    make_truncated(
        os.path.join(mat, "b_int32_16k_mono.wav"),
        os.path.join(mat, "h_truncated.wav"),
    )
    make_fake_wav(os.path.join(mat, "i_fake.wav"))
    make_non_pcm_wav(os.path.join(mat, "j_ulaw.wav"))

    # ---- 非 WAV 扩展名 ----------------------------------------------------
    with open(os.path.join(mat, "k_note.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("剧本备注：此文件不是音频。\n")

    # ---- 8 位与 24 位整数同构素材（分别以各自位深为基准拼接） --------------
    p("u8_x_8k_mono.wav", 0x0001, 1, 8000, 8, 500, freq=261.63)
    p("u8_y_8k_mono.wav", 0x0001, 1, 8000, 8, 600, freq=293.66)
    p("s24_x_48k_stereo.wav", 0x0001, 2, 48000, 24, 400, freq=659.25)
    p("s24_y_48k_stereo.wav", 0x0001, 2, 48000, 24, 350, freq=783.99)

    # ---- 清单 1：综合场景（成功 3 条 + 跳过 9 条 + 缺失路径 1 条） --------
    write_manifest(
        os.path.join(root, "manifest_main.json"),
        [
            "materials/a_int32_16k_mono.wav",          # 成功，基准 1000 帧
            "materials/d_int32_22050_mono.wav",        # 跳过：采样率不同
            "materials/b_int32_16k_mono.wav",          # 成功 1000 帧
            "materials/e_int32_16k_stereo.wav",        # 跳过：声道不同
            "materials/c_float32_16k_mono.wav",        # 成功（浮点转整数）1000 帧
            "materials/f_int16_16k_mono.wav",          # 跳过：位深不同
            "materials/g_bad_declared_length.wav",     # 跳过：声明长度损坏
            "materials/h_truncated.wav",               # 跳过：截断损坏
            "materials/i_fake.wav",                    # 跳过：无法解析为 WAV
            "materials/j_ulaw.wav",                    # 跳过：非 PCM 编码
            "materials/k_note.txt",                    # 跳过：扩展名不符
            "materials/m_missing.wav",                 # 跳过：路径不存在（不生成）
            "materials/a_int32_16k_mono.wav",          # 成功（重复条目）1000 帧
        ],
        "综合清单：参数不一致、损坏、缺失、扩展名不符、浮点转换与重复条目",
    )

    # ---- 清单 2：全部不可用（无结果 WAV，报告逐条说明） --------------------
    write_manifest(
        os.path.join(root, "manifest_all_bad.json"),
        [
            "materials/g_bad_declared_length.wav",  # 损坏
            "materials/h_truncated.wav",            # 截断
            "materials/i_fake.wav",                # 假 WAV
            "materials/j_ulaw.wav",                # 非 PCM
            "materials/k_note.txt",                # 扩展名不符
            "materials/m_missing.wav",             # 缺失
        ],
        "全部片段不可用清单：不得生成结果 WAV",
    )

    # ---- 清单 3：8 位整数同构（含对象式条目写法） -------------------------
    write_manifest(
        os.path.join(root, "manifest_u8.json"),
        [
            {"path": "materials/u8_x_8k_mono.wav", "chapter": "ch01"},
            "materials/u8_y_8k_mono.wav",
        ],
        "8 位 PCM 同构拼接清单（同时演示对象式条目）",
    )

    # ---- 清单 4：24 位立体声同构 ------------------------------------------
    write_manifest(
        os.path.join(root, "manifest_s24.json"),
        [
            "materials/s24_x_48k_stereo.wav",
            "materials/s24_y_48k_stereo.wav",
        ],
        "24 位立体声 PCM 同构拼接清单",
    )

    # ---- 清单 5：片段列表为空 ---------------------------------------------
    write_manifest(
        os.path.join(root, "manifest_empty.json"),
        [],
        "空片段清单：没有任何可用片段，不生成 WAV",
    )

    # ---- 损坏 JSON 与缺字段 JSON（直接写文本，不经 json.dump） ------------
    with open(os.path.join(root, "manifest_broken_json.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write('{"description": "括号不闭合\n')
    with open(os.path.join(root, "manifest_no_segments.json"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write('{"description": "缺少 segments 字段", "clips": []}\n')

    print(f"样本与清单已确定性生成于: {os.path.abspath(root)}")
    print(f"  素材目录: {mat}")
    for name in sorted(os.listdir(root)):
        if name.endswith(".json"):
            print(f"  清单: {name}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "demo"
    main(target)
