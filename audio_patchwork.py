#!/usr/bin/env python3
"""按 JSON 清单顺序拼接 PCM WAV 片段，并生成静音复核报告。

仅使用 Python 3.10+ 标准库。

用法::

    python audio_patchwork.py <manifest.json> [-o OUTPUT_DIR]
                              [--threshold 0.01] [--min-silence 0.1]

设计要点：
- 所有片段先完整校验（存在性、可读性、PCM 编码、采样率/位深/声道一致性）
  之后才开始写文件，因此任何校验失败都不会留下输出半成品。
- 输出音轨先写入同目录临时文件，再以“不覆盖”的方式安装到目标路径。
- 报告同样先写临时文件；只有音轨和报告都准备好后才依次落盘，音轨安装
  失败或报告安装失败时会回滚本次新建的音轨。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

DEFAULT_THRESHOLD = 0.01
DEFAULT_MIN_SILENCE_SECONDS = 0.1

# 各采样位宽下把归一化幅度 [-1.0, 1.0] 映射回整数所需的除数。
_SAMPLE_SCALE = {
    1: 128,          # 8-bit WAV 为无符号，零点在 128
    2: 32768,
    3: 8388608,
    4: 2147483648,
}


class PatchworkError(Exception):
    """可预期的业务错误，由 CLI 统一打印并返回非零退出码。"""


class ManifestError(PatchworkError):
    """清单缺失、不可解析或结构不合法。"""


class EmptyClipListError(PatchworkError):
    """清单片段列表为空。"""


class ClipError(PatchworkError):
    """片段缺失、不可读取或格式不一致。"""


class OutputError(PatchworkError):
    """输出路径冲突或写入失败。"""


@dataclass(frozen=True)
class WavFormat:
    channels: int
    sample_width: int
    frame_rate: int

    @property
    def bit_depth(self) -> int:
        return self.sample_width * 8

    @property
    def frame_size(self) -> int:
        return self.channels * self.sample_width


@dataclass(frozen=True)
class Manifest:
    path: Path
    clips: tuple[str, ...]
    output: str
    report: str | None


@dataclass(frozen=True)
class Clip:
    path: Path
    format: WavFormat
    frames: bytes
    frame_count: int


@dataclass(frozen=True)
class RunResult:
    clip_count: int
    audio_path: Path
    report_path: Path
    silence_segments: tuple[dict[str, float], ...]


# ---------------------------------------------------------------------------
# 清单读取与校验
# ---------------------------------------------------------------------------


def load_manifest(manifest_path: Path) -> Manifest:
    """读取并校验 JSON 清单。任何问题都以 ManifestError 抛出。"""
    path = Path(manifest_path).expanduser().resolve()
    if not path.exists():
        raise ManifestError(f"清单文件不存在: {path}")
    if not path.is_file():
        raise ManifestError(f"清单路径不是文件: {path}")

    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"无法读取清单文件: {path} ({exc})") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"无法解析清单 JSON: {path} "
            f"(第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg})"
        ) from exc

    if not isinstance(document, dict):
        raise ManifestError(f"清单格式无效: 顶层必须是 JSON 对象: {path}")

    clips_raw = document.get("clips")
    if clips_raw is None:
        raise ManifestError(f"清单缺少 clips 字段: {path}")
    if not isinstance(clips_raw, list):
        raise ManifestError(f"清单 clips 字段必须是数组: {path}")

    clips: list[str] = []
    for index, item in enumerate(clips_raw):
        if not isinstance(item, str) or not item.strip():
            raise ManifestError(
                f"清单 clips[{index}] 必须是非空字符串路径: {path}"
            )
        clips.append(item)

    output_raw = document.get("output")
    if not isinstance(output_raw, str) or not output_raw.strip():
        raise ManifestError(f"清单缺少有效的 output 字段（非空字符串）: {path}")

    report_raw = document.get("report")
    if report_raw is not None and (
        not isinstance(report_raw, str) or not report_raw.strip()
    ):
        raise ManifestError(
            f"清单 report 字段必须是非空字符串（或省略）: {path}"
        )

    return Manifest(
        path=path,
        clips=tuple(clips),
        output=output_raw.strip(),
        report=report_raw.strip() if isinstance(report_raw, str) else None,
    )


def _resolve_relative(base_dir: Path, raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def resolve_clip_path(manifest: Manifest, raw: str) -> Path:
    """片段相对路径以清单所在目录为基准解析。"""
    return _resolve_relative(manifest.path.parent, raw)


def resolve_output_path(manifest: Manifest, output_dir: Path) -> Path:
    """输出音轨的相对路径以命令行输出目录（默认当前工作目录）为基准。"""
    return _resolve_relative(Path(output_dir), manifest.output)


def resolve_report_path(
    manifest: Manifest, output_dir: Path, audio_path: Path
) -> Path:
    """报告路径：清单显式指定则使用之，否则为音轨同目录的 <stem>_report.json。"""
    if manifest.report is None:
        return audio_path.with_name(f"{audio_path.stem}_report.json")
    return _resolve_relative(Path(output_dir), manifest.report)


# ---------------------------------------------------------------------------
# 片段读取与一致性校验
# ---------------------------------------------------------------------------


def read_clip(path: Path) -> Clip:
    """读取单个 PCM WAV 片段；非 PCM、损坏或缺失均抛出 ClipError。"""
    if not path.exists():
        raise ClipError(f"片段文件不存在: {path}")
    if not path.is_file():
        raise ClipError(f"片段路径不是文件: {path}")

    try:
        with wave.open(str(path), "rb") as wav_file:
            compression = wav_file.getcomptype()
            if compression != "NONE":
                raise ClipError(
                    f"片段不是 PCM WAV（压缩类型 {compression}）: {path}"
                )
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            if channels <= 0:
                raise ClipError(f"片段声道数无效: {path} (channels={channels})")
            if frame_rate <= 0:
                raise ClipError(f"片段采样率无效: {path} ({frame_rate} Hz)")
            if sample_width not in _SAMPLE_SCALE:
                raise ClipError(
                    f"片段位深不受支持: {path} ({sample_width * 8} bit)；"
                    "仅支持 8/16/24/32 bit PCM"
                )
            data = wav_file.readframes(frame_count)
    except ClipError:
        raise
    except (wave.Error, EOFError, OSError) as exc:
        raise ClipError(f"无法读取 PCM WAV 片段: {path} ({exc})") from exc

    expected_bytes = frame_count * channels * sample_width
    if len(data) != expected_bytes:
        raise ClipError(
            f"片段数据不完整: {path}（声明 {frame_count} 帧，"
            f"实际 {len(data)} 字节，应为 {expected_bytes} 字节）"
        )

    return Clip(
        path=path,
        format=WavFormat(channels, sample_width, frame_rate),
        frames=data,
        frame_count=frame_count,
    )


def validate_clip_formats(clips: Sequence[Clip]) -> None:
    """要求所有片段与首个片段在采样率、位深、声道数上完全一致。"""
    if not clips:
        return

    reference = clips[0]
    ref_format = reference.format
    problems: list[str] = []

    for clip in clips[1:]:
        mismatches: list[str] = []
        if clip.format.frame_rate != ref_format.frame_rate:
            mismatches.append(
                f"采样率不匹配: {clip.format.frame_rate} Hz，"
                f"期望 {ref_format.frame_rate} Hz"
            )
        if clip.format.sample_width != ref_format.sample_width:
            mismatches.append(
                f"位深不匹配: {clip.format.bit_depth} 位，"
                f"期望 {ref_format.bit_depth} 位"
            )
        if clip.format.channels != ref_format.channels:
            mismatches.append(
                f"声道数不匹配: {clip.format.channels}，"
                f"期望 {ref_format.channels}"
            )
        if mismatches:
            detail = "\n".join(f"  - {item}" for item in mismatches)
            problems.append(f"{clip.path}:\n{detail}")

    if problems:
        raise ClipError(
            "片段格式不一致（基准为首个片段 "
            f"{reference.path}，{ref_format.frame_rate} Hz / "
            f"{ref_format.bit_depth} bit / {ref_format.channels} 声道）:\n"
            + "\n".join(problems)
        )


# ---------------------------------------------------------------------------
# 拼接与静音检测
# ---------------------------------------------------------------------------


def concatenate_clips(clips: Sequence[Clip]) -> bytes:
    """按清单顺序连接各片段的原始帧字节（保留声道交错顺序）。"""
    return b"".join(clip.frames for clip in clips)


def _iter_sample_values(data: bytes, sample_width: int) -> Iterator[int]:
    """按小端 PCM 语义逐个产出整数样本。"""
    if sample_width == 1:
        # 8-bit WAV 的样本是无符号字节，调用方负责减去 128。
        yield from data
        return
    if sample_width == 2:
        for (value,) in struct.iter_unpack("<h", data):
            yield value
        return
    if sample_width == 3:
        for offset in range(0, len(data), 3):
            raw = (
                data[offset]
                | (data[offset + 1] << 8)
                | (data[offset + 2] << 16)
            )
            if raw & 0x800000:
                raw -= 0x1000000
            yield raw
        return
    if sample_width == 4:
        for (value,) in struct.iter_unpack("<i", data):
            yield value
        return
    raise ValueError(f"不支持的采样位宽: {sample_width}")


def detect_silence(
    frames: bytes,
    wav_format: WavFormat,
    threshold: float,
    min_duration_seconds: float,
) -> list[dict[str, float]]:
    """检测“所有声道幅度绝对值均低于阈值”的连续帧段。

    仅报告持续时间不短于 ``min_duration_seconds`` 的段。多声道时，只有
    该帧的全部声道样本都低于阈值，才算静音帧，避免把单声道有声误判为静音。
    """
    frame_size = wav_format.frame_size
    total_frames = len(frames) // frame_size
    if total_frames == 0:
        return []

    raw_threshold = threshold * _SAMPLE_SCALE[wav_format.sample_width]
    min_frames = max(
        1, math.ceil(min_duration_seconds * wav_format.frame_rate - 1e-9)
    )

    def make_segment(start_frame: int, end_frame: int) -> dict[str, float]:
        return {
            "start": round(start_frame / wav_format.frame_rate, 3),
            "end": round(end_frame / wav_format.frame_rate, 3),
            "duration": round(
                (end_frame - start_frame) / wav_format.frame_rate, 3
            ),
        }

    segments: list[dict[str, float]] = []
    start_frame: int | None = None
    frame_is_silent = True
    frame_index = 0

    sample_values = _iter_sample_values(
        frames[: total_frames * frame_size], wav_format.sample_width
    )
    for sample_index, value in enumerate(sample_values):
        signed_value = value - 128 if wav_format.sample_width == 1 else value
        if abs(signed_value) >= raw_threshold:
            frame_is_silent = False

        if (sample_index + 1) % wav_format.channels != 0:
            continue

        if frame_is_silent:
            if start_frame is None:
                start_frame = frame_index
        elif start_frame is not None:
            if frame_index - start_frame >= min_frames:
                segments.append(make_segment(start_frame, frame_index))
            start_frame = None
        frame_is_silent = True
        frame_index += 1

    if start_frame is not None and total_frames - start_frame >= min_frames:
        segments.append(make_segment(start_frame, total_frames))

    return segments


# ---------------------------------------------------------------------------
# 输出落盘（先临时文件，后安装）
# ---------------------------------------------------------------------------


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _check_output_absent(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise OutputError(f"输出音轨已存在，拒绝覆盖: {path}")


def _check_parent_dir(path: Path) -> None:
    parent = path.parent
    if parent.exists() and not parent.is_dir():
        raise OutputError(f"输出目录不是目录: {parent}")


def _ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    if parent.exists():
        if not parent.is_dir():
            raise OutputError(f"输出目录不是目录: {parent}")
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputError(f"无法创建输出目录: {parent} ({exc})") from exc


def _write_wav_file(tmp_path: Path, wav_format: WavFormat, frames: bytes) -> None:
    with wave.open(str(tmp_path), "wb") as wav_file:
        wav_file.setnchannels(wav_format.channels)
        wav_file.setsampwidth(wav_format.sample_width)
        wav_file.setframerate(wav_format.frame_rate)
        wav_file.writeframes(frames)


def _write_json_file(tmp_path: Path, payload: dict) -> None:
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _install_without_overwrite(tmp_path: Path, destination: Path) -> None:
    """把临时文件安装到目标路径；目标已存在时绝不覆盖。"""
    _check_output_absent(destination)
    try:
        # 同卷硬链接是原子的“仅当不存在时创建”。
        os.link(tmp_path, destination)
        return
    except FileExistsError as exc:
        raise OutputError(
            f"输出音轨已存在，拒绝覆盖: {destination}"
        ) from exc
    except OSError:
        # 某些文件系统不支持硬链接，回退到独占创建 + 复制。
        pass

    try:
        with open(destination, "xb") as destination_handle, open(
            tmp_path, "rb"
        ) as source_handle:
            shutil.copyfileobj(source_handle, destination_handle)
    except FileExistsError as exc:
        raise OutputError(
            f"输出音轨已存在，拒绝覆盖: {destination}"
        ) from exc
    except OSError as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise OutputError(f"写入输出音轨失败: {destination} ({exc})") from exc


def _install_report(tmp_path: Path, destination: Path) -> None:
    """报告允许在成功运行时原子替换；失败路径不会调用到这里。"""
    try:
        os.replace(tmp_path, destination)
    except OSError as exc:
        raise OutputError(f"写入复核报告失败: {destination} ({exc})") from exc


def _write_outputs(
    audio_path: Path,
    report_path: Path,
    wav_format: WavFormat,
    frames: bytes,
    report_payload: dict,
) -> None:
    tmp_audio: str | None = None
    tmp_report: str | None = None
    audio_installed = False

    try:
        _ensure_parent_dir(audio_path)
        _ensure_parent_dir(report_path)
        _check_output_absent(audio_path)

        fd, tmp_audio = tempfile.mkstemp(
            prefix=".patchwork-", suffix=".wav.tmp", dir=str(audio_path.parent)
        )
        os.close(fd)

        fd, tmp_report = tempfile.mkstemp(
            prefix=".patchwork-", suffix=".json.tmp", dir=str(report_path.parent)
        )
        os.close(fd)

        try:
            _write_wav_file(Path(tmp_audio), wav_format, frames)
        except (OSError, wave.Error, EOFError) as exc:
            raise OutputError(f"写入输出音轨失败: {audio_path} ({exc})") from exc

        try:
            _write_json_file(Path(tmp_report), report_payload)
        except OSError as exc:
            raise OutputError(
                f"写入复核报告失败: {report_path} ({exc})"
            ) from exc

        _install_without_overwrite(Path(tmp_audio), audio_path)
        audio_installed = True
        _install_report(Path(tmp_report), report_path)
    except BaseException:
        # 报告失败或进程级异常时，撤回本次新建的音轨，避免“只有音轨没有报告”。
        if audio_installed:
            try:
                audio_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        for tmp_name in (tmp_audio, tmp_report):
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 业务流程
# ---------------------------------------------------------------------------


def validate_parameters(threshold: float, min_silence_seconds: float) -> None:
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise PatchworkError(
            f"幅度阈值必须在 0 到 1 之间（当前: {threshold}）"
        )
    if not math.isfinite(min_silence_seconds) or min_silence_seconds <= 0.0:
        raise PatchworkError(
            f"最短静音时长必须大于 0 秒（当前: {min_silence_seconds}）"
        )


def execute(
    manifest_path: Path,
    output_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS,
) -> RunResult:
    """执行完整流程；所有校验通过后才写文件。"""
    manifest = load_manifest(manifest_path)
    validate_parameters(threshold, min_silence_seconds)

    if not manifest.clips:
        raise EmptyClipListError("无片段可拼接")

    out_dir = Path(output_dir).expanduser().resolve()
    if out_dir.exists() and not out_dir.is_dir():
        raise OutputError(f"输出目录不是目录: {out_dir}")

    audio_path = resolve_output_path(manifest, out_dir)
    report_path = resolve_report_path(manifest, out_dir, audio_path)

    if _same_path(audio_path, report_path):
        raise OutputError("复核报告路径不能与输出音轨路径相同")
    if _same_path(report_path, manifest.path):
        raise OutputError("复核报告路径不能覆盖清单文件")

    _check_output_absent(audio_path)
    _check_parent_dir(audio_path)
    _check_parent_dir(report_path)

    clip_paths = [resolve_clip_path(manifest, raw) for raw in manifest.clips]
    for clip_path in clip_paths:
        if _same_path(report_path, clip_path):
            raise OutputError(f"复核报告路径不能覆盖输入片段: {clip_path}")

    clips = [read_clip(path) for path in clip_paths]
    validate_clip_formats(clips)

    frames = concatenate_clips(clips)
    wav_format = clips[0].format
    total_frames = sum(clip.frame_count for clip in clips)
    total_duration = round(total_frames / wav_format.frame_rate, 3)

    silence_segments = detect_silence(
        frames, wav_format, threshold, min_silence_seconds
    )
    report_payload = {
        "total_duration": total_duration,
        "sample_rate": wav_format.frame_rate,
        "channels": wav_format.channels,
        "bit_depth": wav_format.bit_depth,
        "clip_count": len(clips),
        "amplitude_threshold": threshold,
        "min_silence_duration": min_silence_seconds,
        "silence_segments": silence_segments,
    }

    _write_outputs(audio_path, report_path, wav_format, frames, report_payload)

    return RunResult(
        clip_count=len(clips),
        audio_path=audio_path,
        report_path=report_path,
        silence_segments=tuple(silence_segments),
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_patchwork.py",
        description="按 JSON 清单顺序拼接 PCM WAV 片段并生成静音复核报告。",
        allow_abbrev=False,
    )
    parser.add_argument("manifest", type=Path, help="JSON 片段清单路径")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path.cwd(),
        help="输出目录；省略时使用当前工作目录",
    )
    parser.add_argument(
        "--threshold",
        "--amplitude-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"静音幅度阈值（归一化 0~1，默认 {DEFAULT_THRESHOLD}）",
    )
    parser.add_argument(
        "--min-silence",
        "--min-silence-duration",
        type=float,
        default=DEFAULT_MIN_SILENCE_SECONDS,
        dest="min_silence",
        help=f"最短静音时长（秒，默认 {DEFAULT_MIN_SILENCE_SECONDS}）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        result = execute(
            manifest_path=args.manifest,
            output_dir=args.output_dir,
            threshold=args.threshold,
            min_silence_seconds=args.min_silence,
        )
    except EmptyClipListError:
        # 验收要求的标准提示，单独打印，避免被错误前缀干扰。
        print("无片段可拼接")
        return 2
    except PatchworkError as exc:
        print(f"错误: {exc}")
        return 1

    print(f"片段数: {result.clip_count}")
    print(f"输出: {result.audio_path}")
    print(f"报告: {result.report_path}")
    print(f"静音段数: {len(result.silence_segments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
