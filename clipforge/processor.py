"""裁剪/拼接主流程：清单校验、源读取、成片写出与 report.json 生成。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import wave
from dataclasses import dataclass
from typing import Any

from . import __version__
from .manifest import (
    ClipEntry,
    load_manifest,
    validate_entries,
    validate_entries_without_source,
)
from .wavio import (
    AtomicWaveWriter,
    AudioError,
    AudioInfo,
    apply_gain_bytes,
    iter_frame_chunks,
    open_pcm_source,
    verify_riff_structure,
)

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INPUT = 2
EXIT_NO_VALID_CLIPS = 3
EXIT_OUTPUT = 4

REPORT_SCHEMA_VERSION = "1.0"


@dataclass
class RunResult:
    exit_code: int
    report: dict[str, Any]
    report_path: str


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_section(info: AudioInfo | None, path: str) -> dict[str, Any]:
    if info is None:
        return {"path": path, "exists": os.path.exists(path), "readable": False}
    return {
        "path": info.path,
        "exists": True,
        "readable": True,
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "sample_width": info.sample_width,
        "bits_per_sample": info.sample_width * 8,
        "total_frames": info.frames,
        "duration_seconds": round(info.duration_seconds, 6),
    }


def _clip_section(entry: ClipEntry, status: str,
                  output_offset: int | None = None) -> dict[str, Any]:
    section: dict[str, Any] = {
        "manifest_index": entry.index,
        "label": entry.label,
        "status": status,
        "start_seconds": entry.start,
        "end_seconds": entry.end,
        "gain": entry.gain if entry.gain is not None else entry.gain_raw,
        "start_frame": entry.start_frame,
        "end_frame": entry.end_frame,
        "frames": entry.frame_count,
    }
    if status == "processed":
        section["output_offset_frame"] = output_offset
        section["reasons"] = []
    else:
        section["output_offset_frame"] = None
        section["reason"] = entry.primary_reason
        section["reasons"] = [err.to_dict() for err in entry.errors]
    return section


def _write_report(report_path: str, report: dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(report_path)) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".clipforge-report-", suffix=".tmp", dir=parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, report_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise AudioError("report_write_error", f"无法写入 report.json：{exc}") from exc


def _new_report(source_path: str, manifest_path: str, output_path: str,
                report_path: str) -> dict[str, Any]:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_by": {"name": "clipforge", "version": __version__},
        "status": "failed",
        "source": _source_section(None, source_path),
        "manifest": {"path": manifest_path, "entry_count": None},
        "output": {
            "path": output_path,
            "created": False,
            "complete": False,
            # 失败时若该路径已有旧文件，标明它是历史遗留，本次未写入。
            "pre_existing": os.path.exists(output_path),
        },
        "report_path": os.path.abspath(report_path),
        "clips": [],
        "summary": {
            "total": 0,
            "processed": 0,
            "unprocessed": 0,
            "processed_indices": [],
            "unprocessed_indices": [],
        },
        "verification": None,
        "errors": [],
    }


def _apply_entries(report: dict[str, Any], entries: list[ClipEntry]) -> None:
    """整次任务失败时，所有条目均为未处理；合法条目补上 run_failed 原因。"""
    clips: list[dict[str, Any]] = []
    for entry in entries:
        section = _clip_section(entry, "unprocessed")
        if entry.valid:
            section["reason"] = "run_failed"
            section["reasons"] = [{
                "code": "run_failed",
                "message": "任务整体失败，该片段未写入成片",
            }]
        clips.append(section)
    report["clips"] = clips
    report["summary"] = {
        "total": len(entries),
        "processed": 0,
        "unprocessed": len(entries),
        "processed_indices": [],
        "unprocessed_indices": [entry.index for entry in entries],
    }


def _finalize_failure(report: dict[str, Any], report_path: str,
                      failures: list[dict[str, str]],
                      exit_code: int = EXIT_INPUT) -> RunResult:
    report["status"] = "failed"
    report["errors"] = failures
    report["output"]["created"] = False
    report["output"]["complete"] = False
    _write_report(report_path, report)
    return RunResult(exit_code=exit_code, report=report, report_path=report_path)


def run(source_path: str, manifest_path: str, output_path: str,
        report_path: str | None = None) -> RunResult:
    """执行一次裁剪拼接任务；无论成功失败都会尽力写出 report.json。"""
    if report_path is None:
        report_path = os.path.join(
            os.path.dirname(os.path.abspath(output_path)), "report.json"
        )
    report = _new_report(source_path, manifest_path, output_path, report_path)

    # 0) 输出不得覆盖任一输入，避免破坏源素材或清单。
    for label, other, code in (
        ("源 WAV", source_path, "output_conflicts_with_source"),
        ("片段清单", manifest_path, "output_conflicts_with_manifest"),
        ("报告路径", report_path, "output_conflicts_with_report"),
    ):
        if _same_path(output_path, other):
            return _finalize_failure(report, report_path, [{
                "code": code, "message": f"输出路径与{label}相同，拒绝覆盖",
            }])

    # 1) 打开源 WAV 并解析清单 JSON；两者都独立校验，失败原因全部报告。
    failures: list[dict[str, str]] = []
    raw_entries: list[Any] = []
    manifest_ok = True
    try:
        raw_entries = load_manifest(manifest_path)
        report["manifest"]["entry_count"] = len(raw_entries)
    except AudioError as exc:
        failures.append({"code": exc.code, "message": exc.message})
        manifest_ok = False

    try:
        wf, info = open_pcm_source(source_path)
    except AudioError as exc:
        failures.append({"code": exc.code, "message": exc.message})
        if manifest_ok:
            # 源不可用时仍逐项给出清单项的静态校验结果。
            _apply_entries(report, validate_entries_without_source(raw_entries))
        else:
            report["clips"] = []
        return _finalize_failure(report, report_path, failures)

    report["source"] = _source_section(info, source_path)
    if not manifest_ok:
        # 清单结构不可用，无法继续；源已确认可读，关闭后按输入错误退出。
        try:
            wf.close()
        except OSError:
            pass
        return _finalize_failure(report, report_path, failures)
    try:
        return _process(wf, info, raw_entries, output_path, report, report_path)
    finally:
        try:
            wf.close()
        except OSError:
            pass


def _process(wf: wave.Wave_read, info: AudioInfo, raw_entries: list[Any],
             output_path: str, report: dict[str, Any],
             report_path: str) -> RunResult:
    entries = validate_entries(raw_entries, info.sample_rate, info.frames)
    valid = [entry for entry in entries if entry.valid]
    invalid = [entry for entry in entries if not entry.valid]

    if not valid:
        _apply_entries(report, entries)
        return _finalize_failure(report, report_path, [{
            "code": "no_valid_clips",
            "message": f"清单共 {len(entries)} 项，全部非法，未生成成片",
        }], exit_code=EXIT_NO_VALID_CLIPS)

    # 3) 按清单顺序逐段读取、加增益、顺序写入；写入走临时文件 + 原子替换。
    clip_sections: list[dict[str, Any]] = []
    total_out_frames = 0
    try:
        with AtomicWaveWriter(output_path, info.sample_rate, info.channels,
                              info.sample_width) as writer:
            for entry in valid:
                assert entry.start_frame is not None
                assert entry.end_frame is not None
                assert entry.gain is not None
                try:
                    wf.setpos(entry.start_frame)
                except (wave.Error, EOFError, OSError) as exc:
                    raise AudioError(
                        "source_read_error",
                        f"无法定位片段 {entry.label} 的起始帧 "
                        f"{entry.start_frame}：{exc}",
                    ) from exc
                data_chunks = iter_frame_chunks(wf, entry.frame_count or 0)
                for chunk, chunk_frames in data_chunks:
                    writer.write_frames(
                        apply_gain_bytes(chunk, info.sample_width, entry.gain),
                        chunk_frames,
                    )
                clip_sections.append(_clip_section(
                    entry, "processed", output_offset=total_out_frames))
                total_out_frames += entry.frame_count or 0
            written_frames = writer.frames_written
    except AudioError as exc:
        # 失败路径不留下成片；所有条目在报告中均为未处理。
        _apply_entries(report, entries)
        code = EXIT_OUTPUT if exc.code == "output_write_error" else EXIT_INPUT
        report["errors"] = [{"code": exc.code, "message": exc.message}]
        report["status"] = "failed"
        _write_report(report_path, report)
        return RunResult(exit_code=code, report=report, report_path=report_path)

    # 4) 写后自检：标准 wave 回读 + RIFF 结构校验，失败则删除成片。
    expected = {
        "channels": info.channels,
        "sample_rate": info.sample_rate,
        "sample_width": info.sample_width,
        "frames": written_frames,
    }
    framesize = info.channels * info.sample_width
    try:
        with wave.open(output_path, "rb") as out_wf:
            actual = {
                "channels": out_wf.getnchannels(),
                "sample_rate": out_wf.getframerate(),
                "sample_width": out_wf.getsampwidth(),
                "frames": out_wf.getnframes(),
            }
            readable_bytes = len(out_wf.readframes(actual["frames"]))
        riff = verify_riff_structure(output_path)
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        try:
            os.unlink(output_path)
        except OSError:
            pass
        _apply_entries(report, entries)
        return _finalize_failure(report, report_path, [{
            "code": "output_verification_failed",
            "message": f"成片写出后自检失败，已删除输出文件：{exc}",
        }])

    if actual != expected or readable_bytes != written_frames * framesize:
        try:
            os.unlink(output_path)
        except OSError:
            pass
        _apply_entries(report, entries)
        return _finalize_failure(report, report_path, [{
            "code": "output_verification_failed",
            "message": f"成片参数与源不一致：期望 {expected}，实际 {actual}；"
                       f"可读字节 {readable_bytes}，期望 {written_frames * framesize}",
        }])

    # 5) 成功：clips 按清单原顺序排列，processed / unprocessed 分明。
    processed_by_index = {section["manifest_index"]: section
                          for section in clip_sections}
    report["clips"] = [
        processed_by_index[entry.index] if entry.valid
        else _clip_section(entry, "unprocessed")
        for entry in entries
    ]
    report["summary"] = {
        "total": len(entries),
        "processed": len(valid),
        "unprocessed": len(invalid),
        "processed_indices": [entry.index for entry in valid],
        "unprocessed_indices": [entry.index for entry in invalid],
    }
    report["output"] = {
        "path": output_path,
        "created": True,
        "exists": True,
        "complete": len(valid) == len(entries),
        "frames": written_frames,
        "duration_seconds": round(written_frames / info.sample_rate, 6),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "sample_width": info.sample_width,
        "sha256": _sha256_file(output_path),
    }
    report["verification"] = {
        "wave_readback": "ok",
        "riff_structure": "ok",
        "params_match_source": True,
        "readable_frames": written_frames,
        "readable_bytes": readable_bytes,
        "riff": riff,
    }
    report["status"] = "success"
    report["errors"] = []
    _write_report(report_path, report)
    return RunResult(exit_code=EXIT_OK, report=report, report_path=report_path)
