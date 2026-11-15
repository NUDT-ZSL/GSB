"""Thin, cancellable wrappers around local ffmpeg and ffprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessError, TaskCancelled


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: str
    ffprobe: str
    ffmpeg_version: str
    ffprobe_version: str


@dataclass(frozen=True)
class ProbeInfo:
    sample_rate: int
    channels: int
    bit_depth: int
    codec_name: str
    duration_seconds: float
    format_name: str


def _run_version(executable: str, label: str) -> str:
    try:
        result = subprocess.run(
            [executable, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BusinessError(
            "DEPENDENCY_MISSING",
            f"{label} 无法启动",
            {"executable": executable, "reason": str(exc)},
        ) from exc
    if result.returncode != 0:
        raise BusinessError(
            "DEPENDENCY_MISSING",
            f"{label} -version 返回非零状态",
            {
                "executable": executable,
                "exit_code": result.returncode,
                "output": (result.stdout or "")[-1000:],
            },
        )
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else label
    return first_line.strip()


def _sibling_tool(ffmpeg: str, name: str) -> str | None:
    path = Path(ffmpeg)
    candidates = [path.with_name(name), path.with_name(name + ".exe")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_toolchain(ffmpeg_path: str | None, ffprobe_path: str | None) -> Toolchain:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    ffprobe = ffprobe_path or shutil.which("ffprobe")
    if not ffprobe and ffmpeg:
        ffprobe = _sibling_tool(ffmpeg, "ffprobe")
    if not ffmpeg:
        raise BusinessError(
            "DEPENDENCY_MISSING",
            "未在 PATH 中找到 ffmpeg，可使用 --ffmpeg 指定",
            {"missing": "ffmpeg"},
        )
    if not ffprobe:
        raise BusinessError(
            "DEPENDENCY_MISSING",
            "未在 PATH 中找到 ffprobe，可使用 --ffprobe 指定",
            {"missing": "ffprobe"},
        )
    ffmpeg_version = _run_version(ffmpeg, "ffmpeg")
    ffprobe_version = _run_version(ffprobe, "ffprobe")
    return Toolchain(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version=ffprobe_version,
    )


def _codec_bit_depth(codec_name: str, explicit: Any) -> int:
    if explicit not in (None, "", "N/A"):
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    codec = codec_name.lower()
    for marker, depth in (
        ("pcm_u8", 8),
        ("pcm_s16", 16),
        ("pcm_u16", 16),
        ("pcm_s24", 24),
        ("pcm_u24", 24),
        ("pcm_s32", 32),
        ("pcm_u32", 32),
        ("pcm_f32", 32),
        ("pcm_f64", 64),
    ):
        if codec.startswith(marker):
            return depth
    return 0


def probe_audio(toolchain: Toolchain, path: Path) -> ProbeInfo:
    command = [
        toolchain.ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        (
            "stream=codec_name,codec_type,sample_rate,channels,bits_per_sample,"
            "bits_per_raw_sample,duration,nb_frames:format=format_name,duration"
        ),
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BusinessError(
            "DECODE_FAILED",
            "ffprobe 执行失败",
            {"file": str(path), "reason": str(exc)},
        ) from exc
    if result.returncode != 0:
        raise BusinessError(
            "DECODE_FAILED",
            "ffprobe 无法读取音频文件",
            {
                "file": str(path),
                "exit_code": result.returncode,
                "stderr": (result.stderr or "")[-2000:],
            },
        )
    try:
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        if not streams:
            raise ValueError("no audio stream")
        stream = streams[0]
        media_format = payload.get("format") or {}
        codec_name = str(stream.get("codec_name") or "")
        sample_rate = int(stream.get("sample_rate") or 0)
        channels = int(stream.get("channels") or 0)
        bit_depth = _codec_bit_depth(
            codec_name,
            stream.get("bits_per_raw_sample", stream.get("bits_per_sample")),
        )
        duration_raw = stream.get("duration", media_format.get("duration"))
        duration = float(duration_raw) if duration_raw not in (None, "N/A") else 0.0
        format_name = str(media_format.get("format_name") or "")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "UNSUPPORTED_FORMAT",
            "无法解析 ffprobe 音频元数据",
            {"file": str(path), "reason": str(exc)},
        ) from exc

    wav_markers = {"wav", "wave", "w64"}
    if not any(marker in format_name.lower() for marker in wav_markers):
        raise BusinessError(
            "UNSUPPORTED_FORMAT",
            "输入文件不是 WAV",
            {"file": str(path), "format_name": format_name},
        )
    if sample_rate <= 0 or channels <= 0 or bit_depth <= 0:
        raise BusinessError(
            "UNSUPPORTED_FORMAT",
            "WAV 缺少有效采样率、声道数或位深",
            {
                "file": str(path),
                "sample_rate": sample_rate,
                "channels": channels,
                "bit_depth": bit_depth,
            },
        )
    return ProbeInfo(
        sample_rate=sample_rate,
        channels=channels,
        bit_depth=bit_depth,
        codec_name=codec_name,
        duration_seconds=duration,
        format_name=format_name,
    )


def _wait_process(
    process: subprocess.Popen[bytes],
    cancel_event: Any | None,
) -> int:
    while True:
        try:
            return process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise TaskCancelled()


def _tail_file(path: Path, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def decode_to_float32(
    toolchain: Toolchain,
    source: Path,
    destination: Path,
    *,
    sample_rate: int,
    channels: int,
    cancel_event: Any | None = None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        toolchain.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-acodec",
        "pcm_f32le",
        "-f",
        "f32le",
        str(destination),
    ]
    _run_ffmpeg_file(
        command,
        stderr_context="音频解码失败",
        cancel_event=cancel_event,
        source=str(source),
    )


def encode_float32_wav(
    toolchain: Toolchain,
    source_raw: Path,
    destination_wav: Path,
    *,
    sample_rate: int,
    channels: int,
    sample_format: str,
    cancel_event: Any | None = None,
) -> None:
    destination_wav.parent.mkdir(parents=True, exist_ok=True)
    command = [
        toolchain.ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        str(source_raw),
        "-c:a",
        sample_format,
        "-f",
        "wav",
        str(destination_wav),
    ]
    _run_ffmpeg_file(
        command,
        stderr_context="WAV 编码失败",
        error_code="OUTPUT_WRITE_FAILED",
        cancel_event=cancel_event,
        source=str(source_raw),
    )


def _run_ffmpeg_file(
    command: list[str],
    *,
    stderr_context: str,
    error_code: str = "DECODE_FAILED",
    cancel_event: Any | None,
    source: str,
) -> None:
    stderr_path: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".ffmpeg-stderr-", suffix=".log")
        stderr_path = Path(tmp_name)
        os.close(fd)
        with stderr_path.open("wb") as stderr_handle:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_handle,
                )
            except OSError as exc:
                raise BusinessError(
                    "DEPENDENCY_MISSING",
                    "FFmpeg 无法启动",
                    {"executable": command[0], "reason": str(exc)},
                ) from exc
            returncode = _wait_process(process, cancel_event)
        if returncode != 0:
            raise BusinessError(
                error_code,
                stderr_context,
                {
                    "source": source,
                    "exit_code": returncode,
                    "stderr": _tail_file(stderr_path),
                },
            )
    finally:
        if stderr_path is not None:
            try:
                stderr_path.unlink()
            except OSError:
                pass
