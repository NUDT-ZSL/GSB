"""批处理流水线：遍历输入目录、按配置顺序处理、写出 WAV+JSON、汇总报告。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import STEP_FADE, STEP_GAIN, STEP_TRIM, PipelineConfig
from .processing import (
    GainInfo,
    StepInfo,
    apply_fade,
    apply_gain,
    quantize_int16,
    trim_silence,
)
from .wav_io import WavData, WavFormatError, read_wav, write_wav


@dataclass
class SuccessEntry:
    rel_path: str           # 相对输入根目录的 POSIX 风格路径，如 a/b.wav
    output_wav: str
    output_json: str
    duration_seconds: float
    steps: list[StepInfo] = field(default_factory=list)


@dataclass
class SkipEntry:
    rel_path: str
    reason: str


@dataclass
class FailureEntry:
    rel_path: str
    reason: str


@dataclass
class ProcessReport:
    input_dir: str
    output_dir: str
    success: list[SuccessEntry] = field(default_factory=list)
    skipped: list[SkipEntry] = field(default_factory=list)
    failed: list[FailureEntry] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.success) + len(self.skipped) + len(self.failed),
            "success": len(self.success),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
        }

    def to_dict(self) -> dict:
        return {
            "input_dir": self.input_dir,
            "output_dir": self.output_dir,
            "summary": self.summary,
            "success": [
                {
                    "path": e.rel_path,
                    "output_wav": e.output_wav,
                    "output_json": e.output_json,
                    "duration_seconds": round(e.duration_seconds, 3),
                    "steps": [{"step": s.step, "detail": s.detail} for s in e.steps],
                }
                for e in self.success
            ],
            "skipped": [{"path": e.rel_path, "reason": e.reason} for e in self.skipped],
            "failed": [{"path": e.rel_path, "reason": e.reason} for e in self.failed],
        }


def _run_steps(
    samples: list[float],
    sample_rate: int,
    config: PipelineConfig,
) -> tuple[list[float], list[StepInfo]]:
    """按配置 order 依次执行三个步骤。"""
    work = [float(s) for s in samples]
    infos: list[StepInfo] = []
    for step in config.order:
        if step == STEP_TRIM:
            work, info = trim_silence(work, sample_rate, config.threshold_db)
        elif step == STEP_FADE:
            work, info = apply_fade(work, sample_rate, config.fade_ms)
        elif step == STEP_GAIN:
            work, info = apply_gain(work, config.gain_db)
        else:  # 配置阶段已校验，这里仅作防御
            raise ValueError(f"未知处理步骤：{step}")
        infos.append(info)
    return work, infos


def _build_metadata(source_name: str, data: WavData) -> dict:
    """引擎可读的同名 JSON 元数据（任务要求的 5 个字段）。"""
    return {
        "source_file": source_name,
        "duration_seconds": round(data.duration_seconds, 3),
        "sample_rate": data.sample_rate,
        "bit_depth": 16,
        "channels": 1,
    }


def process_directory(
    input_dir: str | Path,
    output_dir: str | Path,
    config: PipelineConfig,
) -> ProcessReport:
    """处理整个目录树。单个素材出错只记录、不中断其余素材。"""
    in_root = Path(input_dir)
    out_root = Path(output_dir)
    report = ProcessReport(
        input_dir=str(in_root),
        output_dir=str(out_root),
    )

    if not in_root.is_dir():
        raise NotADirectoryError(f"输入目录不存在或不是目录：{in_root}")

    # 确定性遍历：目录与文件名均排序，报告顺序可复现
    all_files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(in_root):
        dirnames.sort()
        for name in sorted(filenames):
            all_files.append(Path(dirpath) / name)

    for file_path in all_files:
        rel = file_path.relative_to(in_root)
        rel_posix = rel.as_posix()

        # 非 WAV：跳过并说明原因（按扩展名先拦一道，错误信息对业务人员更友好）
        if file_path.suffix.lower() != ".wav":
            report.skipped.append(
                SkipEntry(rel_posix, f"非 WAV 文件（扩展名 {file_path.suffix or '无'}），已跳过")
            )
            continue

        try:
            wav = read_wav(file_path)
            processed, step_infos = _run_steps(wav.samples, wav.sample_rate, config)
            int_samples = quantize_int16(processed)
            out_wav = WavData(samples=int_samples, sample_rate=wav.sample_rate)

            out_path = out_root / rel
            write_wav(out_path, out_wav)

            metadata = _build_metadata(rel.name, out_wav)
            json_path = out_path.with_suffix(".json")
            json_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            report.success.append(
                SuccessEntry(
                    rel_path=rel_posix,
                    output_wav=str(out_path),
                    output_json=str(json_path),
                    duration_seconds=out_wav.duration_seconds,
                    steps=step_infos,
                )
            )
        except WavFormatError as exc:
            # 格式不符 / 文件损坏：跳过，不影响其他素材
            report.skipped.append(SkipEntry(rel_posix, f"WAV 格式不符：{exc}"))
        except OSError as exc:
            report.failed.append(FailureEntry(rel_posix, f"文件读写失败：{exc}"))
        except Exception as exc:  # 防御：任何意外都隔离到当前素材
            report.failed.append(FailureEntry(rel_posix, f"处理失败：{type(exc).__name__}: {exc}"))

    return report


def write_report_file(report: ProcessReport, path: str | Path) -> None:
    """把完整报告落盘为 UTF-8 JSON，方便业务系统/人员归档核查。"""
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
