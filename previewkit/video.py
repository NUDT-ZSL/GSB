"""ffmpeg 封装：把逐帧 RGB 数据无损编码成视频。

选 FFV1（.mkv 容器）而不是 H.264，因为：
  * 无损，抽帧回读的像素与 Pillow 绘制的完全一致，便于「逐帧核对」；
  * 编码过程可复现，同一输入两次运行得到逐字节相同的视频；
  * 不依赖 x264 的线程调度与编码器版本差异。
容器用 Matroska，绝大多数播放器（VLC / mpv / PotPlayer）可直接播放。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from PIL import Image

from .models import EnvironmentProblem

_INFO_HINT = (
    "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
    "或用 --ffmpeg 指定可执行文件路径，"
    "或执行 `pip install imageio-ffmpeg` 使用其内置二进制。"
)


def find_ffmpeg(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise EnvironmentProblem(f"--ffmpeg 指定的文件不存在：{explicit}")
        return str(path)

    env = os.environ.get("FFMPEG_BINARY")
    if env and Path(env).is_file():
        return env

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:  # 可选依赖，装了就优先用它自带的可执行文件
        import imageio_ffmpeg  # type: ignore

        candidate = imageio_ffmpeg.get_ffmpeg_exe()
        if candidate and Path(candidate).is_file():
            return candidate
    except Exception:  # noqa: BLE001 - 任何导入/探测失败都回落到统一报错
        pass

    raise EnvironmentProblem(_INFO_HINT)


def probe_encoders(ffmpeg: str) -> set[str]:
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EnvironmentProblem(f"无法执行 ffmpeg（{ffmpeg}）：{exc}") from exc
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def pick_codec(ffmpeg: str) -> tuple[str, list[str]]:
    available = probe_encoders(ffmpeg)
    if "ffv1" in available:
        # 全帧内（-g 1）保证任意帧可独立解码；不指定 -slices，交给编码器按
        # 单线程默认值处理，避免不同 build 支持的切片数不同。
        return "ffv1", [
            "-level",
            "3",
            "-g",
            "1",
            "-slicecrc",
            "1",
            "-threads",
            "1",
            "-write_crc32",
            "0",
        ]
    if "libx264rgb" in available:  # 退化：无损 RGB 的 H.264
        return "libx264rgb", ["-qp", "0", "-preset", "veryfast", "-threads", "1"]
    if "libx264" in available:
        return "libx264", [
            "-qp",
            "0",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv444p",
            "-threads",
            "1",
        ]
    raise EnvironmentProblem("当前 ffmpeg 不含可用的无损编码器（需 ffv1 或 libx264rgb）。")


def encode(
    ffmpeg: str,
    frames: Iterable[Image.Image],
    fps: int,
    width: int,
    height: int,
    destination: Path,
) -> Path:
    """把帧序列写入 destination；失败时清理半成品。"""
    codec, codec_args = pick_codec(ffmpeg)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.stem + ".part" + destination.suffix)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        codec,
        *codec_args,
        "-r",
        str(fps),
        # bitexact：去掉编码器/封装器写入的时间戳、随机 muxing UID 等，
        # 使同一批帧重复编码得到逐字节相同的文件（需求 13）。
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-map_metadata",
        "-1",
        "-metadata",
        "encoder=",
        "-metadata",
        "creation_time=",
        "-metadata",
        "date=",
        "-metadata",
        "title=",
        str(tmp),
    ]
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    assert proc.stdin is not None
    try:
        for frame in frames:
            if frame.mode != "RGB":
                frame = frame.convert("RGB")
            if frame.size != (width, height):
                frame = frame.resize((width, height), Image.Resampling.NEAREST)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        stderr = proc.stderr.read() if proc.stderr else b""
        code = proc.wait()
    except BrokenPipeError:
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        code = proc.returncode or 1
    except BaseException:
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    if code != 0:
        tmp.unlink(missing_ok=True)
        message = stderr.decode("utf-8", "replace").strip() or f"退出码 {code}"
        raise EnvironmentProblem(f"ffmpeg 编码失败：{message}")
    os.replace(tmp, destination)
    return destination


def decode_frames(ffmpeg: str, source: Path, width: int, height: int) -> Iterable[Image.Image]:
    """按顺序读出所有帧，供抽帧核对与测试使用。尺寸由调用方给出，不再探测。"""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    size = width * height * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield Image.frombytes("RGB", (width, height), buf)
    finally:
        proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        proc.wait()


def count_frames(ffmpeg: str, source: Path) -> int:
    """解码到 null 输出，从 ffmpeg 的进度行里取总帧数。"""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-stats",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise EnvironmentProblem(f"读取视频信息失败：{proc.stderr.strip()[:400]}")
    import re

    matches = re.findall(r"frame=\s*(\d+)", proc.stderr)
    if not matches:
        raise EnvironmentProblem(f"无法统计视频帧数：{source}")
    return int(matches[-1])


def decode_selected(
    ffmpeg: str, source: Path, indices: list[int], width: int, height: int
) -> dict[int, Image.Image]:
    """只解码指定帧号，返回 {帧号: 图像}。"""
    if not indices:
        return {}
    expr = "+".join(f"eq(n\\,{i})" for i in sorted(set(indices)))
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        f"select={expr}",
        "-vsync",
        "0",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise EnvironmentProblem(
            f"抽帧解码失败：{proc.stderr.decode('utf-8', 'replace').strip()[:400]}"
        )
    size = width * height * 3
    wanted = sorted(set(indices))
    data = proc.stdout
    result: dict[int, Image.Image] = {}
    for pos, index in enumerate(wanted):
        chunk = data[pos * size : (pos + 1) * size]
        if len(chunk) < size:
            break
        result[index] = Image.frombytes("RGB", (width, height), chunk)
    return result
