"""视频探测与逐帧解码。

不使用 OpenCV：通过 imageio_ffmpeg 自带的静态 ffmpeg 可执行文件完成解码，
按解码顺序得到确定性的原始 RGB 帧，帧号即解码序号（从 0 起），杜绝凭时间点盲抽。
"""

from __future__ import annotations

import os
import re
import subprocess

from .errors import VideoReadError

# 单次最多解码的原始帧数，防止误传超大视频导致内存失控
MAX_DECODED_FRAMES = 20000

_DIM_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_DURATION_RE = re.compile(
    r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?),")
_SUBPROCESS_FLAGS = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


def get_ffmpeg_exe() -> str:
    """优先使用 imageio_ffmpeg 捆绑的 ffmpeg，缺失时回落到 PATH 中的 ffmpeg。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # pragma: no cover - 兜底分支
        from shutil import which
        exe = which("ffmpeg")
        if exe:
            return exe
        raise VideoReadError(
            "未找到 ffmpeg 可执行文件，请安装 imageio-ffmpeg 或将 ffmpeg 加入 PATH")


def _run_ffmpeg(args: list[str]) -> tuple[bytes, bytes, int]:
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_SUBPROCESS_FLAGS,
            check=False,
        )
    except OSError as exc:
        raise VideoReadError(f"ffmpeg 无法启动：{exc}") from exc
    return proc.stdout, proc.stderr, proc.returncode


def probe_video(video_path: str) -> dict:
    """探测视频，返回 width/height/fps/duration_ms/nb_frames；不可读时抛 VideoReadError。"""
    if not os.path.isfile(video_path):
        raise VideoReadError(f"视频文件不存在：{video_path}")
    if os.path.getsize(video_path) == 0:
        raise VideoReadError(f"视频文件为空，无法解码：{video_path}")

    exe = get_ffmpeg_exe()
    _, stderr, code = _run_ffmpeg([exe, "-hide_banner", "-i", video_path])
    text = stderr.decode("utf-8", errors="replace")
    if code == 0 and "Output #0" not in text and "At least one output" in text:
        pass  # ffmpeg 无输出参数时本就以非零退出，属正常探测

    stream_lines = [line for line in text.splitlines() if "Video:" in line]
    if not stream_lines:
        raise VideoReadError(
            f"文件无法作为视频解码（未检测到视频流）：{video_path}")
    stream = stream_lines[0]

    dim_match = _DIM_RE.search(stream)
    fps_match = _FPS_RE.search(stream)
    if not dim_match or not fps_match:
        raise VideoReadError(f"无法解析视频尺寸或帧率，文件可能已损坏：{video_path}")
    width, height = int(dim_match.group(1)), int(dim_match.group(2))
    fps = float(fps_match.group(1))
    if fps <= 0:
        raise VideoReadError(f"视频帧率非法（{fps}），无法定位帧：{video_path}")

    duration_match = _DURATION_RE.search(text)
    if not duration_match:
        raise VideoReadError(f"无法解析视频时长，文件可能已损坏：{video_path}")
    hh, mm, ss = duration_match.groups()
    duration_seconds = int(hh) * 3600 + int(mm) * 60 + float(ss)
    duration_ms = round(duration_seconds * 1000)

    nb_frames = None
    frame_match = re.search(r"frame=\s*(\d+)", text)
    if frame_match:
        nb_frames = int(frame_match.group(1))
    if not nb_frames:
        nb_frames = max(1, round(duration_seconds * fps))

    return {
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "duration_ms": duration_ms,
        "nb_frames": nb_frames,
    }


def decode_all_frames(video_path: str, info: dict) -> list[bytes]:
    """将整段视频解码为原始 RGB24 字节帧列表（样本视频很小，全量解码最确定）。"""
    width, height = info["width"], info["height"]
    frame_size = width * height * 3
    exe = get_ffmpeg_exe()
    stdout, stderr, _ = _run_ffmpeg([
        exe, "-hide_banner", "-loglevel", "error",
        "-i", video_path,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ])
    if not stdout:
        tail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise VideoReadError(
            f"视频帧解码失败：{video_path}（ffmpeg: {' '.join(tail) or '无输出'}）")
    if len(stdout) % frame_size != 0:
        raise VideoReadError(
            f"解码数据长度 {len(stdout)} 不是单帧字节数 {frame_size} 的整数倍，视频可能已损坏")

    frame_count = len(stdout) // frame_size
    if frame_count > MAX_DECODED_FRAMES:
        raise VideoReadError(
            f"视频帧数 {frame_count} 超过上限 {MAX_DECODED_FRAMES}，请使用更短的切点片段")
    return [stdout[i * frame_size:(i + 1) * frame_size] for i in range(frame_count)]
