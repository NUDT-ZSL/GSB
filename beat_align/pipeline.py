"""离线流水线串联（需求 7、9 的写入顺序约束在这里保证）。

执行顺序::

    读取/校验 WAV  ->  检出鼓点  ->  读取镜头清单  ->  超限标记
    ->  最近鼓点对齐  ->  先写核查结果 JSON  ->  再写预览图  ->  回写预览路径

关键约束：

* WAV 无法读取时在第一步即抛错，此后不会创建任何文件，杜绝剪辑误用旧预览；
* JSON 一定先于预览图落盘；预览目录不可写时返回带 ``preview_error`` 的
  结果，已写出的 JSON 保持完整可读（需求 9）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .align import align_shots
from .audio import AudioInfo, load_wav
from .detect import DEFAULT_FRAME_MS, DEFAULT_HOP_MS, DEFAULT_MIN_GAP_MS, Beat, detect_beats
from .errors import OutputNotWritableError
from .preview import render_all
from .report import build_report, write_report
from .shots import load_shots, mark_overhang


@dataclass
class PipelineResult:
    """一次流水线运行的产物。"""

    audio: AudioInfo
    beats: list[Beat]
    report: dict
    report_path: str
    preview_paths: list[str] = field(default_factory=list)
    preview_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.preview_error is None


def default_report_path(output_dir: str) -> str:
    """核查 JSON 的默认位置：与预览目录同级，避免目录只读时连带无法写 JSON。"""
    abs_dir = os.path.abspath(output_dir)
    parent = os.path.dirname(abs_dir)
    base = os.path.basename(abs_dir.rstrip(os.sep)) or "previews"
    return os.path.join(parent, f"{base}_rhythm_report.json")


def run_pipeline(
    wav_path: str,
    shots_path: str,
    output_dir: str,
    report_path: str | None = None,
    frame_ms: int = DEFAULT_FRAME_MS,
    hop_ms: int = DEFAULT_HOP_MS,
    min_gap_ms: int = DEFAULT_MIN_GAP_MS,
) -> PipelineResult:
    """运行完整对齐流水线，参数与 CLI 一一对应。"""
    # 1) WAV 读取与校验：失败即整体中止，不产生任何输出文件
    audio = load_wav(wav_path)

    # 2) 鼓点检出（升序、已合并重复点）
    beats = detect_beats(
        audio, frame_ms=frame_ms, hop_ms=hop_ms, min_gap_ms=min_gap_ms
    )

    # 3) 镜头清单与累计起点
    entries = load_shots(shots_path)
    overhang = mark_overhang(entries, audio.duration_ms)

    # 4) 对齐。一个鼓点都没检出时，所有镜头如实进入未完成区域（需求 10）
    if not beats:
        for entry in entries:
            if not entry.skipped:
                entry.skipped = True
                entry.skip_reason = "配乐中未检出任何鼓点，无法对齐（请检查检出参数或配乐内容）"
        aligned_shots = []
    else:
        aligned_shots = align_shots(entries, beats)

    detector_params = {
        "frame_ms": frame_ms,
        "hop_ms": hop_ms,
        "min_gap_ms": min_gap_ms,
    }

    # 5) 先写不含预览路径的核查 JSON —— 即使后续预览失败，JSON 依然完整
    report = build_report(
        audio=audio,
        beats=beats,
        entries=entries,
        aligned_shots=aligned_shots,
        overhang=overhang,
        detector_params=detector_params,
        preview_paths=None,
    )
    final_report_path = report_path or default_report_path(output_dir)
    write_report(report, final_report_path)

    # 6) 再写预览图；目录不可写时保留可读 JSON 并明确上报
    try:
        preview_paths = render_all(aligned_shots, beats, audio.duration_ms, output_dir)
    except OutputNotWritableError as exc:
        return PipelineResult(
            audio=audio,
            beats=beats,
            report=report,
            report_path=os.path.abspath(final_report_path),
            preview_paths=[],
            preview_error=str(exc),
        )

    # 7) 预览成功后回写图片路径，重写 JSON
    report = build_report(
        audio=audio,
        beats=beats,
        entries=entries,
        aligned_shots=aligned_shots,
        overhang=overhang,
        detector_params=detector_params,
        preview_paths=preview_paths,
    )
    write_report(report, final_report_path)

    return PipelineResult(
        audio=audio,
        beats=beats,
        report=report,
        report_path=os.path.abspath(final_report_path),
        preview_paths=preview_paths,
    )
