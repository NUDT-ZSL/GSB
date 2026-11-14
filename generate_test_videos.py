# -*- coding: utf-8 -*-
"""
generate_test_videos.py — 生成确定性测试 MP4（无随机数，可重复生成）

生成场景（统一 320x240 @ 30fps，H.264/avc1，通过 OpenCV MSMF 后端编码）:
  1. three_cuts.mp4       4 个镜头 + 3 处硬切（红/绿/蓝/黄，每段 2s）
  2. with_black.mp4       亮镜头 -> 1s 黑场 -> 亮镜头（黑场夹在中间）
  3. static_long.mp4      10s 静态长镜头（同一画面，无切换）
  4. short_one_sec.mp4    1s 非黑短视频（30 帧）
  5. gradient_motion.mp4  6s 连续渐变画面（无硬切，只有缓慢内容变化）
  6. all_black.mp4        3s 全黑视频（边界场景，预期零候选）
  7. corrupted.mp4        前 32 字节是 MP4 头、其后写入随机字节的“损坏文件”

运行:
  python generate_test_videos.py --dir test_assets
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

import cv2
import numpy as np

WIDTH, HEIGHT, FPS = 320, 240, 30


def _label_frame(bgr_value, text, frame_no):
    """纯色底 + 确定性文字标注。"""
    img = np.full((HEIGHT, WIDTH, 3), bgr_value, dtype=np.uint8)
    cv2.putText(img, text, (18, 110), cv2.FONT_HERSHEY_SIMPLEX,
                1.1, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(img, f"f{frame_no:04d}", (18, 170), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (200, 200, 200), 1, cv2.LINE_AA)
    return img


def _gradient_frame(t):
    """随时间缓慢变化的渐变画面（帧间差异小，不构成硬切）。"""
    xs = np.linspace(0, 255, WIDTH, dtype=np.float32)
    shift = 40.0 * t
    row = ((xs + shift) % 255).astype(np.uint8)
    img = np.repeat(row[np.newaxis, :], HEIGHT, axis=0)
    color = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    cv2.putText(color, "gradient", (18, 120), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (255, 255, 255), 2, cv2.LINE_AA)
    return color


def _open_writer(path: str) -> cv2.VideoWriter:
    """优先 MSMF + avc1（真实 H.264），失败则回退默认后端 mp4v。"""
    candidates = [
        (cv2.CAP_MSMF, cv2.VideoWriter_fourcc(*"avc1")),
        (cv2.CAP_ANY, cv2.VideoWriter_fourcc(*"avc1")),
        (cv2.CAP_ANY, cv2.VideoWriter_fourcc(*"mp4v")),
    ]
    last_err = None
    for backend, fourcc in candidates:
        writer = cv2.VideoWriter(path, backend, fourcc, FPS, (WIDTH, HEIGHT))
        if writer.isOpened():
            return writer
        writer.release()
        last_err = f"backend={backend} fourcc={fourcc}"
    raise RuntimeError(f"无可用视频编码器（{last_err}）")


def _write_frames(path: str, frame_iter):
    writer = _open_writer(path)
    count = 0
    try:
        for frame in frame_iter:
            writer.write(frame)
            count += 1
    finally:
        writer.release()
    if count == 0 or not os.path.isfile(path):
        raise RuntimeError(f"视频写出失败: {path}")
    return count


def _verify_h264(path: str, expect_frames: int):
    """回读校验：帧数与编码标签。"""
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"生成后无法打开: {path}")
    tag = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join(chr((tag >> 8 * k) & 0xFF) for k in range(4))
    n = 0
    while cap.read()[0]:
        n += 1
    cap.release()
    if n != expect_frames:
        raise RuntimeError(f"{path}: 期望 {expect_frames} 帧，实际解码 {n} 帧")
    return codec, n


def build_all(out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    summary = {}

    # 1) 三处硬切、四个镜头，每段 60 帧（2s），共 240 帧
    path = os.path.join(out_dir, "three_cuts.mp4")
    shots = [((60, 60, 200), "shot0 red"),
             ((60, 180, 60), "shot1 green"),
             ((200, 90, 40), "shot2 blue"),
             ((40, 200, 220), "shot3 yellow")]

    def gen_three_cuts():
        fno = 0
        for color, text in shots:
            for _ in range(60):
                yield _label_frame(color, text, fno)
                fno += 1

    n = _write_frames(path, gen_three_cuts())
    summary["three_cuts.mp4"] = (*_verify_h264(path, n), "4 镜头 / 3 硬切")

    # 2) 亮-黑-亮：各 45 帧（1.5s），黑场 30 帧（1s），共 120 帧
    path = os.path.join(out_dir, "with_black.mp4")

    def gen_with_black():
        fno = 0
        for _ in range(45):
            yield _label_frame((90, 140, 210), "lit A", fno); fno += 1
        for _ in range(30):
            yield np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8); fno += 1
        for _ in range(45):
            yield _label_frame((180, 110, 70), "lit B", fno); fno += 1

    n = _write_frames(path, gen_with_black())
    summary["with_black.mp4"] = (*_verify_h264(path, n), "含 1s 黑场")

    # 3) 10s 静态长镜头：300 帧完全相同的画面
    path = os.path.join(out_dir, "static_long.mp4")
    static_img = _label_frame((120, 120, 120), "static long take", 0)

    def gen_static():
        for _ in range(FPS * 10):
            yield static_img.copy()

    n = _write_frames(path, gen_static())
    summary["static_long.mp4"] = (*_verify_h264(path, n), "10s 静态单镜头")

    # 4) 1s 非黑短视频：30 帧
    path = os.path.join(out_dir, "short_one_sec.mp4")

    def gen_short():
        for fno in range(FPS):
            yield _label_frame((70, 130, 90), "short clip", fno)

    n = _write_frames(path, gen_short())
    summary["short_one_sec.mp4"] = (*_verify_h264(path, n), "1s 短视频")

    # 5) 6s 连续渐变、无硬切：180 帧
    path = os.path.join(out_dir, "gradient_motion.mp4")

    def gen_gradient():
        for fno in range(FPS * 6):
            yield _gradient_frame(fno / float(FPS * 6))

    n = _write_frames(path, gen_gradient())
    summary["gradient_motion.mp4"] = (*_verify_h264(path, n), "6s 渐变无硬切")

    # 6) 3s 全黑：90 帧
    path = os.path.join(out_dir, "all_black.mp4")
    black = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)

    def gen_all_black():
        for _ in range(FPS * 3):
            yield black.copy()

    n = _write_frames(path, gen_all_black())
    summary["all_black.mp4"] = (*_verify_h264(path, n), "3s 全黑（零候选预期）")

    # 7) 损坏文件：复制正常文件头部后覆写垃圾字节（确定性内容）
    good = os.path.join(out_dir, "short_one_sec.mp4")
    path = os.path.join(out_dir, "corrupted.mp4")
    shutil.copyfile(good, path)
    rng = np.random.default_rng(seed=20261114)
    size = os.path.getsize(path)
    garbage = rng.integers(0, 256, size=size, dtype=np.uint8).tobytes()
    with open(path, "wb") as fp:
        with open(good, "rb") as src:
            fp.write(src.read(32))  # 保留 32 字节 ftyp 头，伪装成 mp4
        fp.write(garbage[32:])
    summary["corrupted.mp4"] = ("n/a", 0, "损坏文件，预期解码失败")

    return summary


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="生成确定性测试 MP4")
    parser.add_argument("--dir", default="test_assets", help="输出目录")
    args = parser.parse_args()
    summary = build_all(os.path.abspath(args.dir))
    print(f"测试素材目录: {os.path.abspath(args.dir)}")
    print(f"{'文件':<22}{'编码':<8}{'解码帧数':<10}说明")
    for name, (codec, frames, note) in summary.items():
        print(f"{name:<22}{codec:<8}{frames:<10}{note}")


if __name__ == "__main__":
    main()
