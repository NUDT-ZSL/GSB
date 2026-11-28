from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .config import Config


def build_low_confidence_intervals(
    frames: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    phonemes: list[dict[str, Any]],
    unmapped_notes: list[dict[str, Any]],
    config: Config,
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    if frames:
        hop = config.hop_length_s
        current_start: float | None = None
        current_end = 0.0
        current_ids: list[int] = []
        for frame in frames:
            is_low = bool(
                frame["voiced"]
                and float(frame["voicing_confidence"]) < config.low_confidence_voicing
            )
            if is_low:
                if current_start is None:
                    current_start = float(frame["timestamp_s"]) - 0.5 * hop
                current_end = float(frame["timestamp_s"]) + 0.5 * hop
                current_ids.append(int(frame["index"]))
            elif current_start is not None:
                intervals.append(
                    {
                        "kind": "low_confidence_frames",
                        "start_s": round(current_start, 6),
                        "end_s": round(current_end, 6),
                        "frame_indices": current_ids,
                        "reason": "voicing confidence below threshold",
                    }
                )
                current_start = None
                current_ids = []
        if current_start is not None:
            intervals.append(
                {
                    "kind": "low_confidence_frames",
                    "start_s": round(current_start, 6),
                    "end_s": round(current_end, 6),
                    "frame_indices": current_ids,
                    "reason": "voicing confidence below threshold",
                }
            )
    for note in notes:
        if float(note["confidence"]) < config.low_confidence_note:
            intervals.append(
                {
                    "kind": "low_confidence_note",
                    "start_s": note["start_s"],
                    "end_s": note["end_s"],
                    "ids": [note["id"]],
                    "reason": "note confidence below threshold",
                }
            )
    for phoneme in phonemes:
        if phoneme["low_confidence"]:
            intervals.append(
                {
                    "kind": "low_confidence_phoneme",
                    "start_s": phoneme["start_s"],
                    "end_s": phoneme["end_s"],
                    "ids": [phoneme["id"]],
                    "reason": "phoneme category/voicing consistency below threshold",
                }
            )
    for note in unmapped_notes:
        intervals.append(
            {
                "kind": "unmapped_note",
                "start_s": note["start_s"],
                "end_s": note["end_s"],
                "ids": [note["note_id"]],
                "reason": note["reason"],
            }
        )
    intervals.sort(key=lambda item: (float(item["start_s"]), float(item["end_s"]), item["kind"]))
    return intervals


def _clean_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _flatten_frame(frame: dict[str, Any]) -> dict[str, Any]:
    acf = frame.get("acf_candidate") or {}
    cep = frame.get("cepstrum_candidate") or {}
    return {
        "index": frame["index"],
        "timestamp_s": frame["timestamp_s"],
        "f0_hz": frame["f0_hz"],
        "midi": frame["midi"],
        "voiced": frame["voiced"],
        "voicing_confidence": frame["voicing_confidence"],
        "method": frame["method"],
        "selection_reason": frame["selection_reason"],
        "raw_f0_hz": frame["raw_f0_hz"],
        "raw_midi": frame["raw_midi"],
        "octave_corrected": frame["octave_corrected"],
        "octave_corrected_f0_hz": frame["octave_corrected_f0_hz"],
        "smoothing_window": frame["smoothing_window"],
        "energy_dbfs": frame["energy_dbfs"],
        "zero_crossing_rate": frame["zero_crossing_rate"],
        "acf_f0_hz": acf.get("f0_hz"),
        "acf_quality": acf.get("quality"),
        "cepstrum_f0_hz": cep.get("f0_hz"),
        "cepstrum_quality": cep.get("quality"),
    }


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _clean_value(row.get(key)) for key in fieldnames})


def write_reports(
    report: dict[str, Any],
    output_dir: str | Path,
    evaluation: dict[str, Any] | None = None,
) -> dict[str, str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    if evaluation is not None:
        payload["evaluation"] = evaluation
    json_path = output / "report.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=_clean_value)
        handle.write("\n")

    frame_rows = [_flatten_frame(frame) for frame in report["frames"]]
    _write_csv(
        output / "frames.csv",
        frame_rows,
        list(frame_rows[0].keys())
        if frame_rows
        else [
            "index",
            "timestamp_s",
            "f0_hz",
            "midi",
            "voiced",
            "voicing_confidence",
            "method",
            "selection_reason",
        ],
    )
    note_fields = [
        "index",
        "id",
        "start_s",
        "end_s",
        "duration_s",
        "midi",
        "note_name",
        "f0_hz",
        "confidence",
        "mean_energy_dbfs",
        "voiced_ratio",
        "start_evidence",
        "end_evidence",
        "boundary_strength",
        "mapped",
        "syllable_index",
        "mapping_role",
        "melisma_position",
        "vowel_overlap_s",
    ]
    _write_csv(output / "notes.csv", report["notes"], note_fields)
    phoneme_fields = [
        "id",
        "syllable_index",
        "text",
        "pinyin",
        "label",
        "kind",
        "start_s",
        "end_s",
        "duration_s",
        "confidence",
        "alignment_cost",
        "category_affinity",
        "low_confidence",
    ]
    _write_csv(output / "phonemes.csv", report["phonemes"], phoneme_fields)
    syllable_fields = [
        "index",
        "text",
        "pinyin",
        "phrase_index",
        "start_s",
        "end_s",
        "vowel_start_s",
        "mapped",
        "note_ids",
        "primary_note_id",
        "mapping_confidence",
        "low_confidence",
        "phoneme_ids",
    ]
    syllable_rows = []
    for syllable in report["syllables"]:
        row = dict(syllable)
        row["note_ids"] = ";".join(syllable["note_ids"])
        row["phoneme_ids"] = ";".join(syllable["phoneme_ids"])
        syllable_rows.append(row)
    _write_csv(output / "syllables.csv", syllable_rows, syllable_fields)
    score_fields = [
        "reference_index",
        "note_id",
        "reference_pitch",
        "reference_midi",
        "detected_midi",
        "detected_note",
        "cents_deviation",
        "absolute_cents",
        "pitch_hit",
        "direction",
        "reference_start_s",
        "reference_end_s",
        "detected_start_s",
        "detected_end_s",
        "onset_error_ms",
        "offset_error_ms",
        "duration_error_ms",
        "temporal_overlap_ratio",
    ]
    _write_csv(output / "scoring.csv", report["scoring"]["notes"], score_fields)
    _write_csv(
        output / "unmapped_notes.csv",
        report["unmapped_notes"],
        ["note_id", "note_index", "start_s", "end_s", "midi", "confidence", "reason"],
    )
    markdown_path = output / "report.md"
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    return {
        "report_json": str(json_path),
        "report_markdown": str(markdown_path),
        "frames_csv": str(output / "frames.csv"),
        "notes_csv": str(output / "notes.csv"),
        "phonemes_csv": str(output / "phonemes.csv"),
        "syllables_csv": str(output / "syllables.csv"),
        "scoring_csv": str(output / "scoring.csv"),
        "unmapped_notes_csv": str(output / "unmapped_notes.csv"),
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    scoring = report["scoring"]["summary"]
    lines: list[str] = []
    lines.append(f"# 声乐课后反馈报告：{report['sample_id']}")
    lines.append("")
    lines.append("## 总体摘要")
    lines.append("")
    lines.append(f"- 音频时长：{_fmt(summary['duration_s'], 3)} s")
    lines.append(f"- 分析帧数：{summary['frame_count']}，浊音帧：{summary['voiced_frame_count']}")
    lines.append(f"- 检出音符：{summary['note_count']}，歌词音节：{summary['syllable_count']}")
    lines.append(f"- 音准命中率：{_fmt(scoring['pitch_hit_rate'] * 100.0, 1)}%")
    lines.append(f"- 节奏稳定分：{_fmt(scoring['rhythm_stability_score'], 1)} / 100")
    lines.append(
        f"- 音节映射：{summary['mapped_syllable_count']}/{summary['syllable_count']}"
        f"（{'一致' if summary['syllable_count_matches_text'] else '不一致'}）"
    )
    if summary["warnings"]:
        lines.append(f"- 警告：{'；'.join(summary['warnings'])}")
    lines.append("")

    lines.append("## 逐句讲评")
    lines.append("")
    for phrase in report["phrases"]:
        lines.append(f"- {phrase['human_summary']}")
    lines.append("")

    lines.append("## 音符与音准")
    lines.append("")
    lines.append("| 音符 | 起止(s) | 音高 | 音分偏差 | 判定 | 置信度 |")
    lines.append("|---|---|---|---|---|---|")
    score_by_note = {
        match["note_id"]: match
        for match in report["scoring"]["notes"]
        if match.get("note_id") is not None
    }
    for note in report["notes"]:
        match = score_by_note.get(note["id"])
        cents = _fmt(match["cents_deviation"], 1) if match else "-"
        verdict = (
            ("命中" if match["pitch_hit"] else "未命中") + f"/{match['direction']}"
            if match
            else "无参考"
        )
        lines.append(
            f"| {note['id']} | {note['start_s']:.3f}-{note['end_s']:.3f} | "
            f"{note['note_name']} | {cents} | {verdict} | {note['confidence']:.2f} |"
        )
    lines.append("")

    lines.append("## 音节与音素对齐")
    lines.append("")
    lines.append("| 音节 | 拼音 | 主音符 | 元音起点(s) | 音素（起-止） |")
    lines.append("|---|---|---|---|---|")
    phoneme_by_id = {phoneme["id"]: phoneme for phoneme in report["phonemes"]}
    for syllable in report["syllables"]:
        phoneme_text = ", ".join(
            f"{phoneme_by_id[phoneme_id]['label']} "
            f"{phoneme_by_id[phoneme_id]['start_s']:.3f}-{phoneme_by_id[phoneme_id]['end_s']:.3f}"
            for phoneme_id in syllable["phoneme_ids"]
            if phoneme_id in phoneme_by_id
        )
        lines.append(
            f"| {syllable['text']} | {syllable['pinyin']} | "
            f"{syllable['primary_note_id'] or '-'} | "
            f"{_fmt(syllable['vowel_start_s'])} | {phoneme_text} |"
        )
    lines.append("")

    lines.append("## 低置信度与未映射区间")
    lines.append("")
    if report["low_confidence_intervals"]:
        for interval in report["low_confidence_intervals"]:
            lines.append(
                f"- {interval['kind']}：{interval['start_s']:.3f}-{interval['end_s']:.3f} s，"
                f"{interval['reason']}"
            )
    else:
        lines.append("- 无")
    lines.append("")

    lines.append("## 未映射音符")
    lines.append("")
    if report["unmapped_notes"]:
        for note in report["unmapped_notes"]:
            lines.append(
                f"- {note['note_id']}：{note['start_s']:.3f}-{note['end_s']:.3f} s，"
                f"{note['reason']}"
            )
    else:
        lines.append("- 无")
    lines.append("")

    evaluation = report.get("evaluation")
    if evaluation:
        lines.append("## 手工标注验收")
        lines.append("")
        lines.append(f"- 总体：{'通过' if evaluation['passed'] else '未通过'}")
        frame_metrics = evaluation["frame_metrics"]
        note_metrics = evaluation["note_metrics"]
        phoneme_metrics = evaluation["phoneme_metrics"]
        lines.append(
            f"- 基频命中率：{frame_metrics['pitch_hit_rate_reference_voiced'] * 100.0:.1f}%，"
            f"浊音 F1：{frame_metrics['voiced_f1']:.3f}"
        )
        lines.append(
            f"- 音符边界 MAE：{_fmt(note_metrics['boundary_mae_ms'])} ms，"
            f"F1：{note_metrics['f1']:.3f}"
        )
        lines.append(
            f"- 音素边界 MAE：{_fmt(phoneme_metrics['boundary_mae_ms'])} ms，"
            f"命中率：{phoneme_metrics['boundary_hit_rate'] * 100.0:.1f}%"
        )
        lines.append(
            f"- 音节映射一致：{'是' if evaluation['syllable_mapping']['consistent'] else '否'}"
        )
        lines.append("")
    return "\n".join(lines)

