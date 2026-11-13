#!/usr/bin/env python3
"""podcutter - 单声道 16 位 PCM WAV 播客裁剪器（仅标准库）。

用法:
    python podcutter.py --wav episode.wav --cuts cuts.json --out clips --report report.json

行为摘要:
    * 输入 WAV 必须是单声道 / 16 位 / PCM；否则整体报错，不写任何片段。
    * 裁剪列表中每个对象必须含 name / start / end，可选 gain（dB，默认 0）。
    * name 仅允许字母、数字、下划线，列表内不得重复；否则整体配置错误，终止。
    * 单个片段范围非法只跳过该片段，不影响其他片段。
    * 输出文件名为 <name>.wav，同名旧文件会被本次结果覆盖（原子替换）。
    * 报告始终按输入顺序列出每个片段；配置/文件级错误写入顶层 error。
"""

from __future__ import annotations

import argparse
import array
import json
import math
import os
import re
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
REQUIRED_FIELDS = ("name", "start", "end")
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_TERMINAL = 2
EXIT_REPORT_WRITE_FAILED = 3


class PodcutterError(Exception):
    """终止性错误：配置错误、文件错误或输入格式不支持。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ClipSpec:
    index: int          # 1-based，用于报告中的可读定位
    name: str
    start: float
    end: float
    gain_db: float


@dataclass(frozen=True)
class AudioData:
    path: str
    sample_rate: int
    total_frames: int

    @property
    def duration_seconds(self) -> float:
        return self.total_frames / self.sample_rate


# ---------------------------------------------------------------------------
# 输入检查与配置解析
# ---------------------------------------------------------------------------

def _abs(path: str | os.PathLike[str]) -> str:
    return os.path.abspath(os.fspath(path))


def _check_inputs(wav_path: str, cuts_path: str) -> None:
    """确认 WAV 与 JSON 都存在、是普通文件且可读。一次报告所有问题。"""
    problems: list[str] = []
    missing = False
    for path, label in ((wav_path, "输入 WAV"), (cuts_path, "裁剪列表 JSON")):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            missing = True
            problems.append(f"{label} 不存在: {path}")
            continue
        except OSError as exc:
            problems.append(f"{label} 无法访问: {path} ({exc})")
            continue
        if not os.path.isfile(path):
            problems.append(f"{label} 不是普通文件: {path}")
            continue
        try:
            with open(path, "rb") as fh:
                fh.read(1)
        except OSError as exc:
            problems.append(f"{label} 不可读: {path} ({exc})")
    if problems:
        code = "FILE_NOT_FOUND" if missing else "FILE_NOT_READABLE"
        raise PodcutterError(code, "；".join(problems))


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def parse_cuts(cuts_path: str) -> list[ClipSpec]:
    """读取并整体校验裁剪列表。任何配置问题都抛出终止性错误。"""
    try:
        raw = Path(cuts_path).read_bytes()
    except OSError as exc:
        raise PodcutterError("FILE_NOT_READABLE", f"裁剪列表 JSON 不可读: {cuts_path} ({exc})")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PodcutterError("INVALID_JSON", f"裁剪列表不是有效的 UTF-8 文本: {exc}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PodcutterError(
            "INVALID_JSON",
            f"裁剪列表 JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}",
        )

    if not isinstance(data, list):
        raise PodcutterError("CONFIG_ERROR", "裁剪列表 JSON 顶层必须是数组（每个元素为一个片段对象）")

    specs: list[ClipSpec] = []
    seen: dict[str, int] = {}
    for idx, item in enumerate(data, start=1):
        where = f"第 {idx} 个片段"
        if not isinstance(item, dict):
            raise PodcutterError("CONFIG_ERROR", f"{where}必须是 JSON 对象")

        missing_fields = [field for field in REQUIRED_FIELDS if field not in item]
        if missing_fields:
            raise PodcutterError(
                "CONFIG_ERROR",
                f"{where}缺少必需字段: {', '.join(missing_fields)}",
            )

        name = item["name"]
        if not isinstance(name, str):
            raise PodcutterError("CONFIG_ERROR", f"{where}的 name 必须是字符串")
        if not NAME_PATTERN.fullmatch(name):
            raise PodcutterError(
                "CONFIG_ERROR",
                f"{where}的 name {name!r} 非法：只允许字母、数字和下划线",
            )
        if name in seen:
            raise PodcutterError(
                "CONFIG_ERROR",
                f"{where}的 name {name!r} 重复（与第 {seen[name]} 个片段同名）",
            )
        seen[name] = idx

        for field in ("start", "end"):
            if not _is_number(item[field]):
                raise PodcutterError(
                    "CONFIG_ERROR",
                    f"{where}的 {field} 必须是有限数字（int/float），实际为 {type(item[field]).__name__}",
                )

        gain = item.get("gain", 0)
        if not _is_number(gain):
            raise PodcutterError(
                "CONFIG_ERROR",
                f"{where}的 gain 必须是有限数字（dB），实际为 {type(gain).__name__}",
            )

        specs.append(
            ClipSpec(
                index=idx,
                name=name,
                start=float(item["start"]),
                end=float(item["end"]),
                gain_db=float(gain),
            )
        )
    return specs


def read_pcm_wav(wav_path: str) -> AudioData:
    """校验 WAV 头；仅接受单声道 16 位 PCM。帧数据在写出每个片段时读取。"""
    try:
        with wave.open(wav_path, "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            sample_rate = wf.getframerate()
            comptype = wf.getcomptype()
            nframes = wf.getnframes()

            if channels != 1 or sample_width != 2 or comptype != "NONE":
                raise PodcutterError(
                    "UNSUPPORTED_WAV_FORMAT",
                    "输入 WAV 必须为单声道 16 位 PCM；"
                    f"实际为 {channels} 声道、{sample_width * 8} 位、编码 {comptype}",
                )
            if sample_rate <= 0:
                raise PodcutterError("INVALID_WAV", "输入 WAV 的采样率无效")
    except PodcutterError:
        raise
    except wave.Error as exc:
        message = str(exc)
        if "unknown format" in message.lower():
            raise PodcutterError(
                "UNSUPPORTED_WAV_FORMAT",
                f"输入 WAV 不是 PCM 编码，无法裁剪: {message}",
            )
        raise PodcutterError("INVALID_WAV", f"无法解析 WAV 文件: {message}")
    except (EOFError, OSError) as exc:
        raise PodcutterError("INVALID_WAV", f"读取 WAV 文件失败: {exc}")

    return AudioData(
        path=_abs(wav_path),
        sample_rate=sample_rate,
        total_frames=nframes,
    )


# ---------------------------------------------------------------------------
# 片段处理
# ---------------------------------------------------------------------------

def apply_gain(raw: bytes, gain_db: float) -> bytes:
    """对 16 位小端 PCM 施加增益并做饱和裁剪；0 dB 时原样返回。"""
    if gain_db == 0.0:
        return raw

    factor = 10.0 ** (gain_db / 20.0)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()

    out = array.array(
        "h",
        (
            max(-32768, min(32767, int(round(sample * factor))))
            for sample in samples
        ),
    )
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _base_entry(spec: ClipSpec) -> dict[str, Any]:
    # 键顺序即报告中的阅读顺序（JSON 保留插入顺序）。
    return {
        "name": spec.name,
        "status": None,
        "output_file": None,
        "duration_seconds": None,
        "error": None,
        "start": spec.start,
        "end": spec.end,
        "gain_db": spec.gain_db,
    }


def _write_segment(
    out_dir: Path,
    spec: ClipSpec,
    audio: AudioData,
    start_sample: int,
    end_sample: int,
) -> Path:
    """原子写出一个片段；失败时清理临时文件并抛出异常。"""
    frame_count = end_sample - start_sample
    expected_bytes = frame_count * 2
    try:
        with wave.open(audio.path, "rb") as wf:
            wf.setpos(start_sample)
            raw = wf.readframes(frame_count)
    except (wave.Error, EOFError, OSError) as exc:
        raise OSError(f"读取输入 WAV 帧数据失败: {exc}") from exc
    if len(raw) != expected_bytes:
        raise OSError(
            f"输入 WAV 在采样点 {start_sample}..{end_sample} 处数据不完整"
            f"（期望 {expected_bytes} 字节，实际 {len(raw)} 字节）"
        )

    payload = apply_gain(raw, spec.gain_db)
    target = out_dir / f"{spec.name}.wav"

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".podcutter-{spec.name}-", suffix=".wav", dir=str(out_dir)
    )
    os.close(fd)
    try:
        with wave.open(tmp_name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(audio.sample_rate)
            wf.writeframes(payload)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def _resolve_sample_range(
    spec: ClipSpec, audio: AudioData
) -> tuple[int, int]:
    duration = audio.duration_seconds
    if not (spec.start >= 0 and spec.start < spec.end and spec.end <= duration):
        raise ValueError(
            "片段范围无效："
            f"start={spec.start:g}s, end={spec.end:g}s, "
            f"文件时长={duration:.6f}s；要求 0 <= start < end <= 文件时长"
        )

    start_sample = int(round(spec.start * audio.sample_rate))
    end_sample = int(round(spec.end * audio.sample_rate))
    start_sample = max(0, min(start_sample, audio.total_frames))
    end_sample = max(0, min(end_sample, audio.total_frames))
    if start_sample >= end_sample:
        raise ValueError(
            "片段范围无效：按采样率换算后不足 1 个采样点"
            f"（start_sample={start_sample}, end_sample={end_sample}）"
        )
    return start_sample, end_sample


def _summary(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"success": 0, "failed": 0, "invalid": 0, "skipped": 0}
    for entry in entries:
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
    return {
        "total": len(entries),
        "success": counts["success"],
        "failed": counts["failed"],
        "invalid": counts["invalid"],
        "skipped": counts["skipped"],
    }


def _new_report(
    wav_path: str, cuts_path: str, out_dir: str
) -> dict[str, Any]:
    return {
        "status": "error",
        "error": None,
        "inputs": {
            "wav": _abs(wav_path),
            "cuts": _abs(cuts_path),
            "output_dir": _abs(out_dir),
        },
        "audio": None,
        "segments": [],
        "summary": {"total": 0, "success": 0, "failed": 0, "invalid": 0, "skipped": 0},
    }


def cut_podcast(
    wav_path: str,
    cuts_path: str,
    out_dir: str,
    report_path: str | None = None,
) -> dict[str, Any]:
    """执行裁剪并返回报告字典；若提供 report_path 则同时写出 JSON 报告。"""
    report = _new_report(wav_path, cuts_path, out_dir)
    specs: list[ClipSpec] = []

    try:
        _check_inputs(wav_path, cuts_path)
        specs = parse_cuts(cuts_path)
        audio = read_pcm_wav(wav_path)

        report["audio"] = {
            "sample_rate": audio.sample_rate,
            "channels": 1,
            "bits_per_sample": 16,
            "total_frames": audio.total_frames,
            "duration_seconds": audio.duration_seconds,
        }

        target_dir = Path(out_dir)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PodcutterError(
                "OUTPUT_DIR_ERROR", f"无法创建输出目录 {out_dir}: {exc}"
            )
        if not target_dir.is_dir():
            raise PodcutterError(
                "OUTPUT_DIR_ERROR", f"输出路径不是目录: {out_dir}"
            )

        used_outputs: dict[str, str] = {}
        entries: list[dict[str, Any]] = []
        for spec in specs:
            entry = _base_entry(spec)
            entries.append(entry)
            try:
                start_sample, end_sample = _resolve_sample_range(spec, audio)
            except ValueError as exc:
                entry["status"] = "invalid"
                entry["error"] = str(exc)
                continue

            target = target_dir / f"{spec.name}.wav"
            collision_key = os.path.normcase(str(target))
            if collision_key in used_outputs:
                entry["status"] = "failed"
                entry["error"] = (
                    f"输出文件名与片段 {used_outputs[collision_key]!r} 冲突"
                    "（文件系统不区分大小写），本片段未写出"
                )
                continue

            try:
                _write_segment(target_dir, spec, audio, start_sample, end_sample)
            except Exception as exc:  # 单片段失败不得影响后续片段
                detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                entry["status"] = "failed"
                entry["error"] = f"写出片段失败: {detail}"
                continue

            used_outputs[collision_key] = spec.name
            entry["status"] = "success"
            entry["output_file"] = target.name
            entry["duration_seconds"] = (end_sample - start_sample) / audio.sample_rate
            entry["start_sample"] = start_sample
            entry["end_sample"] = end_sample
            entry["frames"] = end_sample - start_sample

        report["segments"] = entries
        report["summary"] = _summary(entries)
        report["status"] = (
            "success"
            if all(entry["status"] == "success" for entry in entries)
            else "partial"
        )

    except PodcutterError as exc:
        report["error"] = {"code": exc.code, "message": exc.message}
        report["status"] = "error"
        if exc.code in {"UNSUPPORTED_WAV_FORMAT", "INVALID_WAV"} and specs:
            # 格式不支持时也按输入顺序列出片段，明确它们全部被跳过。
            reason = (
                "输入 WAV 格式不支持，未生成任何输出"
                if exc.code == "UNSUPPORTED_WAV_FORMAT"
                else "输入 WAV 无法解析，未生成任何输出"
            )
            entries = []
            for spec in specs:
                entry = _base_entry(spec)
                entry["status"] = "skipped"
                entry["error"] = reason
                entries.append(entry)
            report["segments"] = entries
            report["summary"] = _summary(entries)
        else:
            report["segments"] = []
            report["summary"] = _summary([])

    if report_path is not None:
        write_report(report_path, report)
    return report


# ---------------------------------------------------------------------------
# 报告写出与 CLI
# ---------------------------------------------------------------------------

def write_report(report_path: str, report: dict[str, Any]) -> None:
    path = Path(report_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"无法创建报告目录 {path.parent}: {exc}") from exc

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _print_report(report: dict[str, Any], report_path: str) -> None:
    print(f"报告文件: {report_path}")
    print(f"整体状态: {report['status']}")
    if report.get("error"):
        print(f"错误 [{report['error']['code']}]: {report['error']['message']}")
    audio = report.get("audio")
    if audio:
        print(
            "输入音频: "
            f"{audio['sample_rate']} Hz, {audio['channels']} 声道, "
            f"{audio['bits_per_sample']} 位, {audio['duration_seconds']:.3f}s"
        )
    summary = report["summary"]
    print(
        f"片段统计: 共 {summary['total']}，成功 {summary['success']}，"
        f"失败 {summary['failed']}，无效 {summary['invalid']}，"
        f"跳过 {summary['skipped']}"
    )
    for entry in report["segments"]:
        if entry["status"] == "success":
            print(
                f"  [success] {entry['name']} -> {entry['output_file']} "
                f"({entry['duration_seconds']:.3f}s, gain={entry['gain_db']:g} dB)"
            )
        else:
            print(f"  [{entry['status']}] {entry['name']}: {entry['error']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="将单声道 16 位 PCM WAV 按时段裁剪并生成 JSON 报告"
    )
    parser.add_argument("--wav", required=True, help="输入 WAV 文件")
    parser.add_argument("--cuts", required=True, help="裁剪列表 JSON 文件")
    parser.add_argument("--out", required=True, help="输出目录（不存在会自动创建）")
    parser.add_argument(
        "--report", default="report.json", help="报告 JSON 路径（默认 report.json）"
    )
    args = parser.parse_args(argv)

    try:
        report = cut_podcast(args.wav, args.cuts, args.out, args.report)
    except OSError as exc:
        print(f"无法写出报告 {args.report}: {exc}", file=sys.stderr)
        return EXIT_REPORT_WRITE_FAILED

    _print_report(report, args.report)
    if report["status"] == "error":
        return EXIT_TERMINAL
    if report["status"] == "partial":
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
