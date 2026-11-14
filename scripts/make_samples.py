"""确定性生成样本视频、切点 CSV 与联系表配置。

视频由 ffmpeg lavfi 纯色源合成（320x180、10fps、共 3 秒 30 帧），
相同 ffmpeg 二进制 + 固定参数产出字节一致的 MP4，便于重建与版本比对。

时间轴（每 100ms 一帧）：
    帧 0..2   红      帧 3..5   绿      帧 6..10  蓝
    帧 11..14 黄(a)   帧 15..19 黄(b，与 a 同画面)   帧 20..29 青
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import imageio_ffmpeg

FPS = 10
WIDTH, HEIGHT = 320, 180
DURATION_S = 3.0
# (颜色名, hex, 时长秒) —— 两段黄色画面相同，用于近似合并核对
SEGMENTS = [
    ("red",    "red",    0.3),
    ("green",  "green",  0.3),
    ("blue",   "blue",   0.5),
    ("yellow", "yellow", 0.4),
    ("yellow2", "yellow", 0.5),
    ("cyan",   "cyan",   1.0),
]
_SUBPROCESS_FLAGS = 0x08000000 if os.name == "nt" else 0


def _write_csv(path: str, rows: list[tuple[int, int]]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("frame,ms\n")
        for frame, ms in rows:
            fh.write(f"{frame},{ms}\n")


def generate(sample_dir: str) -> None:
    os.makedirs(sample_dir, exist_ok=True)
    video_path = os.path.join(sample_dir, "sample.mp4")

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    inputs: list[str] = []
    for _, color, duration in SEGMENTS:
        inputs += ["-f", "lavfi", "-i",
                   f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}:d={duration}"]
    concat = "".join(f"[{idx}:v]" for idx in range(len(SEGMENTS)))
    filter_complex = (
        f"{concat}concat=n={len(SEGMENTS)}:v=1:a=0[v];"
        f"[v]format=yuv420p[vout]"
    )
    cmd = [
        exe, "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "23",
        "-pix_fmt", "yuv420p",
        video_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=_SUBPROCESS_FLAGS, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"样本视频生成失败：{proc.stderr.decode('utf-8', errors='replace')}")

    # 正常清单：五色各一个代表帧
    _write_csv(os.path.join(sample_dir, "cuts_normal.csv"), [
        (1, 100),    # 红
        (4, 400),    # 绿
        (8, 800),    # 蓝
        (12, 1200),  # 黄 a
        (22, 2200),  # 青
    ])

    # 近似合并清单：帧 12 与帧 13 同为纯黄、间隔 100ms（默认阈值即可触发合并）
    _write_csv(os.path.join(sample_dir, "cuts_merge.csv"), [
        (1, 100),
        (4, 400),
        (12, 1200),
        (13, 1300),  # 与帧 12 画面相同且相邻
        (22, 2200),
    ])

    # 异常清单：重复帧号 + 逆序/相等毫秒（坏行也保留一条）
    with open(os.path.join(sample_dir, "cuts_anomalies.csv"),
              "w", encoding="utf-8", newline="\n") as fh:
        fh.write("frame,ms\n")
        fh.write("2,200\n")
        fh.write("5,500\n")
        fh.write("5,700\n")    # 帧号重复
        fh.write("9,600\n")    # 毫秒逆序
        fh.write("12,1200\n")
        fh.write("20,1200\n")  # 毫秒未严格递增（相等）
        fh.write("bad,900\n")  # 坏行：帧号非整数

    # 越界清单：时长 3000ms，含帧号与毫秒双重越界
    _write_csv(os.path.join(sample_dir, "cuts_out_of_range.csv"), [
        (1, 100),
        (40, 4000),   # 毫秒越界且帧号越界
        (25, 9999),   # 毫秒越界
    ])

    # 空清单：仅表头
    _write_csv(os.path.join(sample_dir, "cuts_empty.csv"), [])

    # 非视频文件：纯文本伪装成 mp4
    with open(os.path.join(sample_dir, "not_a_video.mp4"),
              "w", encoding="utf-8", newline="\n") as fh:
        fh.write("this is plain text, not a video container\n")

    # 默认配置
    with open(os.path.join(sample_dir, "config_default.json"),
              "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "thumb_width": 320,
            "thumb_height": 180,
            "columns": 4,
            "spacing": 12,
            "padding": 24,
            "font_size": 14,
            "label_template": "帧 {frame} | {ms} ms"
        }, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # 覆盖颜色 + 非法尺寸边界对照（验证脚本还会临时构造越界配置）
    with open(os.path.join(sample_dir, "config_custom_color.json"),
              "w", encoding="utf-8", newline="\n") as fh:
        json.dump({
            "thumb_width": 240,
            "thumb_height": 135,
            "columns": 3,
            "spacing": 8,
            "padding": 16,
            "font_size": 12,
            "background_color": "#0E1116",
            "grid_color": "#FFD400",
            "text_color": "#FFFFFF"
        }, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"样本已生成于 {os.path.abspath(sample_dir)}")
    for name in sorted(os.listdir(sample_dir)):
        print("  -", name)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
    generate(target)
