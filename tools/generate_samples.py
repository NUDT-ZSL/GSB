"""确定性样本生成脚本（仅标准库）。

运行：
    python tools/generate_samples.py            # 默认输出到 samples/
    python tools/generate_samples.py --dir DIR

生成内容（内容完全确定，可重复运行、逐字节复现）：
- sample.wav：单声道、8000Hz、16bit PCM，时长 4 秒（32000 个采样点）。
  每个采样点取值为 (i * 7) % 65536 - 32768，即覆盖全部 16bit 取值的
  确定性线性斜坡，便于测试通过采样点取值反查切片偏移。
- marks.csv：无表头、UTF-8、三列（编号,起始秒,结束秒），覆盖
  成功 / 浮点毫秒对齐 / 越界 / 无效区间 / 重叠 / 大小写混合编号等场景。
"""

from __future__ import annotations

import argparse
import csv
import os
import struct
import sys
import wave

# 让脚本在仓库任意工作目录下都能直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2          # 16 bit
CHANNELS = 1
DURATION_SECONDS = 4
FRAME_COUNT = SAMPLE_RATE * DURATION_SECONDS  # 32000

# (编号, 起始秒, 结束秒, 预期状态说明)
MARK_ROWS = [
    ("A", "0.0", "1.0", "成功"),
    ("b", "1.5", "3.0", "成功（报告排序演示：A 排在 b 前）"),
    ("c", "1.2345", "1.4", "成功，毫秒对齐：起点量化为 1.235s -> 采样点 9880"),
    ("D", "3.2", "3.8", "成功"),
    ("E", "-1", "5", "越界：音频仅 4 秒"),
    ("F", "2", "2", "无效区间：结束等于起始"),
    ("G", "0.5", "0.8", "重叠：与片段 A[0,1] 端点区间相交，后出现者跳过"),
]


def generate_wav(path: str) -> None:
    pcm = bytearray()
    for i in range(FRAME_COUNT):
        # 确定性 16bit 有符号线性斜坡
        value = (i * 7) % 65536 - 32768
        pcm += struct.pack("<h", value)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(pcm))


def generate_csv(path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        for segment_id, start, end, _note in MARK_ROWS:
            writer.writerow([segment_id, start, end])


def main() -> int:
    parser = argparse.ArgumentParser(description="生成确定性 WAV/CSV 样本")
    parser.add_argument("--dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples"))
    args = parser.parse_args()

    os.makedirs(args.dir, exist_ok=True)
    wav_path = os.path.join(args.dir, "sample.wav")
    csv_path = os.path.join(args.dir, "marks.csv")
    generate_wav(wav_path)
    generate_csv(csv_path)

    print(f"已生成 WAV：{wav_path}")
    print(f"  单声道 / {SAMPLE_RATE} Hz / 16 bit / {FRAME_COUNT} 帧 / "
          f"{DURATION_SECONDS} 秒")
    print(f"已生成 CSV：{csv_path}")
    print("标记预期：")
    for segment_id, start, end, note in MARK_ROWS:
        print(f"  {segment_id}\t[{start}, {end}]\t{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
