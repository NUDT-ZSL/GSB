"""Markdown work-order report."""

from __future__ import annotations

from pathlib import Path

from .grading import GRADE_NAMES_ZH
from .workflow import WORKFLOW_LABELS


DAMAGE_LABELS = {
    "clipping": "削波",
    "dc_offset": "直流偏移",
    "noise": "底噪抬升",
    "impulse": "脉冲爆音",
}


def _num(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _sec(sample: int | None, sample_rate: int) -> str:
    if sample is None:
        return "-"
    return f"{int(sample) / float(sample_rate):.3f}s"


def _grade(value: str | None) -> str:
    return GRADE_NAMES_ZH.get(value or "clean", value or "-")


def _interval_metric(damage_type: str, item: dict) -> str:
    if damage_type == "clipping":
        return (
            f"覆盖 {100.0 * float(item.get('segment_coverage', 0.0)):.2f}% / "
            f"帧内削波 {100.0 * float(item.get('clip_frame_ratio', 0.0)):.1f}% / "
            f"峰值 {_num(item.get('peak_ratio_max'), 4)} / "
            f"平顶 {int(item.get('plateau_samples_max', 0))} 样本"
        )
    if damage_type == "dc_offset":
        return (
            f"abs(DC) {_num(item.get('abs_dc_offset'), 5)} "
            f"({_num(item.get('median_dc_offset'), 5)}, {item.get('polarity', '-')})"
        )
    if damage_type == "noise":
        return (
            f"中位噪声底 {_num(item.get('median_noise_floor_dbfs'), 2)} dBFS / "
            f"高于基线 {_num(item.get('elevation_db'), 2)} dB"
        )
    return (
        f"峰值 {_num(item.get('peak_amplitude'), 4)} / "
        f"突跳比 {_num(item.get('jump_ratio'), 2)} / "
        f"突跳幅度 {_num(item.get('jump_amplitude'), 4)} / "
        f"持续 {_num(item.get('duration_seconds', 0.0) * 1000.0, 2)} ms"
    )


def _interval_subtype(damage_type: str, item: dict) -> str:
    if damage_type == "clipping":
        return {"hard": "硬削波", "soft": "轻度限幅", "mixed": "混合"}.get(
            item.get("subtype"), item.get("subtype", "-")
        )
    if damage_type == "impulse":
        return "瞬态"
    return "-"


def _frame_evidence_table(frames: list[dict]) -> list[str]:
    lines = [
        "| 帧 | 起点 | 终点 | 完整 | 峰值 | 平顶样本 | DC | 噪声底 | 真值噪声 | 误差dB | 突跳比 | 频谱平坦度 | 标签 |",
        "|---:|---:|---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for frame in frames:
        labels = []
        if frame.get("clipping_label") and frame["clipping_label"] != "none":
            labels.append(f"clip:{frame['clipping_label']}")
        if frame.get("impulse_candidate"):
            labels.append("impulse")
        lines.append(
            f"| {frame.get('frame_index')} | {frame.get('start_sample')} | {frame.get('end_sample')} | "
            f"{'是' if frame.get('complete') else '否'} | {_num(frame.get('peak_ratio'), 4)} | "
            f"{frame.get('plateau_samples')} | {_num(frame.get('dc_offset'), 5)} | "
            f"{_num(frame.get('noise_floor_dbfs'), 2)} | {_num(frame.get('truth_noise_floor_dbfs'), 2)} | "
            f"{_num(frame.get('noise_error_db'), 2)} | {_num(frame.get('diff_jump_ratio'), 2)} | "
            f"{_num(frame.get('impulse_flatness'), 3)} | {','.join(labels) or '-'} |"
        )
    return lines


def _segment_section(
    recording: dict,
    segment: dict,
    sample_rate: int,
    tolerances: dict[str, float],
) -> list[str]:
    lines: list[str] = []
    lines.append(f"#### {segment['segment_id']}")
    lines.append("")
    lines.append(
        f"- 区间：样本 {segment['start_sample']}–{segment['end_sample']} "
        f"({_sec(segment['start_sample'], sample_rate)}–{_sec(segment['end_sample'], sample_rate)})；"
        f"帧 {segment['frame_count']}（完整 {segment['complete_frame_count']}，"
        f"不完整 {segment['incomplete_frame_count']}）"
    )
    lines.append(
        f"- 录音段等级：**{_grade(segment['overall_grade'])}**；"
        f"依据：{segment['overall_grade_rationale']}"
    )
    lines.append(
        f"- 建议流程：**{segment['workflow']['label']}** "
        f"(`{segment['workflow']['code']}`)；{segment['workflow']['rationale']}"
    )
    lines.append(
        "- 真值核对："
        + (
            "无法核对（生成器未提供该段真值，仅报告检测结果）"
            if segment["verification"]["status"] == "unverifiable"
            else "**"
            + {
                "verified": "一致",
                "inconsistent": "不一致",
            }.get(segment["verification"]["status"], segment["verification"]["status"])
            + "**"
        )
    )
    lines.append("")

    lines.append("**受损区间与分级**")
    lines.append("")
    lines.append(
        "| 类型 | 子类型 | 起点样本 | 终点样本 | 时长 | 分级依据指标 | 分级 | 复核 |"
    )
    lines.append("|---|---|---:|---:|---:|---|---|:---:|")
    interval_count = 0
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        for item in segment["damage_intervals"][damage_type]:
            interval_count += 1
            grade_value = item.get("grade")
            if damage_type == "impulse":
                grade_value = segment["segment_grades"]["impulse"]
            review = ""
            for flag in segment.get("manual_review", []):
                if flag["metric"] in ("segment_clip_coverage", "segment_clip_coverage_total") and damage_type == "clipping":
                    review = "是"
                if flag["metric"] == "abs_median_dc_offset" and damage_type == "dc_offset":
                    review = "是"
                if flag["metric"] == "median_noise_floor_dbfs" and damage_type == "noise":
                    review = "是"
                if flag["metric"].startswith("impulse_") and damage_type == "impulse":
                    review = "是"
            lines.append(
                f"| {DAMAGE_LABELS[damage_type]} | {_interval_subtype(damage_type, item)} | "
                f"{item['start_sample']} | {item['end_sample']} | "
                f"{_num(item.get('duration_seconds', 0.0), 3)}s | "
                f"{_interval_metric(damage_type, item)} | {_grade(grade_value)} | {review} |"
            )
    if interval_count == 0:
        lines.append("| 无 | - | - | - | - | - | 无 | - |")
    lines.append("")

    lines.append("| 类型 | 段级等级 | 分级指标 |")
    lines.append("|---|---|---|")
    lines.append(
        f"| 削波 | {_grade(segment['segment_grades']['clipping'])} | "
        f"段覆盖 {100.0 * segment['segment_clip_coverage_total']:.2f}% |"
    )
    dc_values = [float(item["abs_dc_offset"]) for item in segment["damage_intervals"]["dc_offset"]]
    lines.append(
        f"| 直流偏移 | {_grade(segment['segment_grades']['dc_offset'])} | "
        f"最大 abs(DC) {_num(max(dc_values) if dc_values else None, 5)} |"
    )
    noise_values = [
        float(item["median_noise_floor_dbfs"]) for item in segment["damage_intervals"]["noise"]
    ]
    lines.append(
        f"| 底噪 | {_grade(segment['segment_grades']['noise'])} | "
        f"中位噪声底 {_num(max(noise_values) if noise_values else None, 2)} dBFS |"
    )
    lines.append(
        f"| 脉冲爆音 | {_grade(segment['segment_grades']['impulse'])} | "
        f"{len(segment['damage_intervals']['impulse'])} 个 / "
        f"{_num(segment['impulse_density_per_second'], 3)} 个/秒 / "
        f"峰值 {_num(segment['impulse_peak_amplitude'], 4)} |"
    )
    lines.append("")

    if segment.get("manual_review"):
        lines.append("**人工复核（分级阈值临界带）**")
        lines.append("")
        for flag in segment["manual_review"]:
            lines.append(f"- {flag['condition']}")
        lines.append("")

    verification = segment["verification"]
    if verification["status"] == "unverifiable":
        lines.append("**真值核对**")
        lines.append("")
        lines.append(f"- {verification['reason']}")
        lines.append(
            f"- 检测区间计数：{verification['detected_interval_counts']}"
        )
        lines.append("")
        return lines

    lines.append("**真值核对明细**")
    lines.append("")
    lines.append("| 类型 | 真值数 | 检测数 | 匹配 | 漏报 | 误报 | 最大边界偏差 | 容许偏差 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        item = verification["interval_results"][damage_type]
        lines.append(
            f"| {DAMAGE_LABELS[damage_type]} | {item['truth_count']} | {item['detected_count']} | "
            f"{item['matched_count']} | {item['false_negative_count']} | "
            f"{item['false_positive_count']} | {_num(item['max_boundary_deviation_ms'], 1)} ms | "
            f"{_num(item['boundary_tolerance_ms'], 1)} ms |"
        )
    lines.append("")
    grade_bits = []
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        item = verification["grade_results"][damage_type]
        grade_bits.append(
            f"{DAMAGE_LABELS[damage_type]} 真值 {_grade(item['truth_grade'])} / "
            f"检测 {_grade(item['detected_grade'])} ({'一致' if item['match'] else '不一致'})"
        )
    lines.append("- 分级一致性：" + "；".join(grade_bits))
    lines.append(
        f"- 段级等级：真值 {_grade(verification['overall_grade']['truth'])} / "
        f"检测 {_grade(verification['overall_grade']['detected'])}；"
        f"建议流程：真值 `{verification['workflow']['truth']}` / "
        f"检测 `{verification['workflow']['detected']}` "
        f"({'一致' if verification['workflow']['match'] else '不一致'})"
    )
    noise = verification.get("noise_floor", {})
    if noise.get("status") == "compared":
        lines.append(
            f"- 自适应噪声底：MAE {_num(noise['mae_db'], 3)} dB，"
            f"RMSE {_num(noise['rmse_db'], 3)} dB，"
            f"6 dB 内帧比例 {_num(noise['within_tolerance_fraction'], 3)}；"
            f"全帧 RMS 直接估计 MAE {_num(noise.get('naive_frame_rms_mae_db'), 3)} dB，"
            f"自适应改善 {_num(noise.get('adaptive_improvement_db'), 3)} dB"
        )
    if verification["issues"]:
        lines.append("- 不一致项：" + "；".join(
            f"{item['kind']}({item.get('type', '-')})" for item in verification["issues"]
        ))
    else:
        lines.append("- 区间、分级与流程核对：全部一致")
    lines.append("")
    if verification.get("first_issue_kind") and verification["status"] == "inconsistent":
        lines.append(
            f"**首个不一致证据**：`{verification['first_issue_kind']}`，"
            f"锚点样本 {verification.get('first_issue_anchor_sample')}"
        )
        lines.append("")
        lines.extend(_frame_evidence_table(verification.get("frame_evidence", [])))
        lines.append("")
    return lines


def build_markdown_report(payload: dict, manifest_path: str | Path) -> str:
    primary = payload["primary"]
    comparison = payload["comparison"]
    dual = payload["dual_frame_comparison"]
    acceptance = payload["acceptance"]
    sample_rate = int(primary["parameters"]["sample_rate"] or 1)
    tolerances = dual["tolerances_ms"]

    lines: list[str] = []
    lines.append("# 音频档案质量评估报告")
    lines.append("")
    lines.append("## 1. 工单抬头")
    lines.append("")
    lines.append(f"- 录音清单：`{manifest_path}`")
    lines.append(
        f"- 生成器：`{primary['manifest'].get('generator')}`；固定种子 "
        f"`{primary['manifest'].get('seed')}`；采样率 `{sample_rate}` Hz；单声道"
    )
    lines.append(
        f"- 主帧参数：帧长 `{primary['parameters']['frame_length_samples']}` 样本 "
        f"({_num(primary['parameters']['frame_length_seconds'] * 1000.0, 3)} ms)，"
        f"帧移 `{primary['parameters']['hop_samples']}` 样本 "
        f"({_num(primary['parameters']['hop_seconds'] * 1000.0, 3)} ms)"
    )
    lines.append(
        f"- 对照帧参数：帧长 `{comparison['parameters']['frame_length_samples']}` 样本 "
        f"({_num(comparison['parameters']['frame_length_seconds'] * 1000.0, 3)} ms)，"
        f"帧移 `{comparison['parameters']['hop_samples']}` 样本 "
        f"({_num(comparison['parameters']['hop_seconds'] * 1000.0, 3)} ms)"
    )
    fractional_bits = []
    for label, analysis in (("主参数", primary), ("对照参数", comparison)):
        requested = analysis["parameters"].get("requested", {})
        for key, name in (("frame_length", "帧长"), ("hop", "帧移")):
            item = requested.get(key, {})
            if item.get("fractional"):
                fractional_bits.append(
                    f"{label}{name} `{item.get('raw')}` -> "
                    f"`{analysis['parameters'][key + '_samples']}` 样本"
                )
    if fractional_bits:
        lines.append(
            "- 参数取整：" + "；".join(fractional_bits)
            + "。非整数样本已四舍五入，段尾仍按实际剩余样本数标记不完整帧。"
        )
    lines.append(
        f"- 规模：录音 {primary['summary']['recording_count']} 条，"
        f"录音段 {primary['summary']['segment_count']} 段，"
        f"帧 {primary['summary']['frame_count']}（不完整帧 "
        f"{primary['summary']['incomplete_frame_count']}）；采样点均为全局坐标，"
        f"受损区间按半开区间 `[start, end)` 记"
    )
    lines.append(
        f"- 总体结论：验收 **{'通过' if acceptance['pass'] else '未通过'}**；"
        f"真值核验 {primary['summary']['verified_segments']}/"
        f"{primary['summary']['truth_available_segments']} 段一致，"
        f"真值缺失 {primary['summary']['unverifiable_segments']} 段；"
        f"双帧长 {'稳定' if dual['stable'] else '存在需复核差异'}"
    )
    if payload.get("frames_csv"):
        lines.append(
            f"- 帧级明细：`{payload['frames_csv'].get('primary')}`、"
            f"`{payload['frames_csv'].get('comparison')}`"
        )
    lines.append("")

    lines.append("## 2. 录音级汇总")
    lines.append("")
    lines.append("| 录音 | 标题 | 段数 | 录音级等级 | 建议流程 | 真值核验 |")
    lines.append("|---|---|---:|---|---|---|")
    for recording in primary["recordings"]:
        vs = recording["verification_summary"]
        lines.append(
            f"| `{recording['recording_id']}` | {recording.get('title', '')} | "
            f"{recording['segment_count']} | {_grade(recording['overall_grade'])} | "
            f"{WORKFLOW_LABELS[recording['workflow']['code']]} | "
            f"{vs['verified_segments']} 一致 / {vs['inconsistent_segments']} 不一致 / "
            f"{vs['unverifiable_segments']} 无法核对 |"
        )
    lines.append("")

    lines.append("## 3. 分级阈值（互不覆盖）")
    lines.append("")
    lines.append("| 类型 | 指标 | 轻 | 中 | 重 | 说明 |")
    lines.append("|---|---|---|---|---|---|")
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        item = primary.get("grade_thresholds", {}).get(damage_type, {})
        if not item and payload.get("grade_thresholds"):
            item = payload["grade_thresholds"].get(damage_type, {})
        lines.append(
            f"| {DAMAGE_LABELS[damage_type]} | {item.get('metric', '-')} | "
            f"{item.get('light', '-')} | {item.get('medium', '-')} | {item.get('heavy', '-')} | "
            f"{item.get('note', '-')} |"
        )
    lines.append("")
    lines.append(
        "分级采用半开区间；临界带默认取阈值附近约 ±15%（底噪 ±2 dB、脉冲峰值 ±0.035/0.06），"
        "命中临界带的指标在段内单独标记人工复核。"
    )
    lines.append("")

    lines.append("## 4. 逐段评估")
    lines.append("")
    for recording in primary["recordings"]:
        lines.append(f"### {recording['recording_id']} — {recording.get('title', '')}")
        lines.append("")
        lines.append(
            f"录音级等级 **{_grade(recording['overall_grade'])}**；"
            f"建议流程 **{WORKFLOW_LABELS[recording['workflow']['code']]}**；"
            f"{recording['overall_grade_rationale']}。"
        )
        lines.append("")
        for segment in recording["segments"]:
            lines.extend(_segment_section(recording, segment, sample_rate, tolerances))

    lines.append("## 5. 双帧长稳定性核对")
    lines.append("")
    lines.append(
        f"两套参数均保留完整结果，未以其中一套覆盖另一套。"
        f"稳定段 {dual['stable_segment_count']}/{dual['segment_count']}；"
        f"需复核段 {dual['unstable_segment_count']}。"
    )
    lines.append("")
    lines.append("| 类型 | 最大起止偏差 | 段级容许偏差 |")
    lines.append("|---|---:|---:|")
    for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
        lines.append(
            f"| {DAMAGE_LABELS[damage_type]} | "
            f"{_num(dual['max_boundary_deviation_ms_by_type'][damage_type], 1)} ms | "
            f"{_num(dual['tolerances_ms'][damage_type], 1)} ms |"
        )
    lines.append("")
    lines.append("| 录音段 | 主参数等级/流程 | 对照参数等级/流程 | 噪声底中位差 | 稳定 |")
    lines.append("|---|---|---|---:|:---:|")
    for item in dual["segments"]:
        lines.append(
            f"| `{item['segment_id']}` | {_grade(item.get('overall_grade_primary'))} / "
            f"`{item.get('workflow_primary')}` | {_grade(item.get('overall_grade_comparison'))} / "
            f"`{item.get('workflow_comparison')}` | "
            f"{_num(item.get('noise_floor_median_delta_db'), 2)} dB | "
            f"{'是' if item.get('stable') else '否'} |"
        )
    lines.append("")
    if not dual["stable"]:
        lines.append("**双帧长不一致明细与建议复核段落**")
        lines.append("")
        for item in dual["segments"]:
            if item.get("stable"):
                continue
            lines.append(f"- `{item['segment_id']}`：")
            for difference in item.get("differences", []):
                lines.append(f"  - {difference}")
        lines.append("")
        lines.append(
            "- 建议复核段落：" + "、".join(f"`{sid}`" for sid in dual["review_segment_ids"])
        )
        lines.append("")

    lines.append("## 6. 首个不一致段落与帧级证据")
    lines.append("")
    first_bad = None
    for recording in primary["recordings"]:
        for segment in recording["segments"]:
            if segment["verification"]["status"] == "inconsistent":
                first_bad = segment
                break
        if first_bad:
            break
    if first_bad is None:
        lines.append(
            "所有真值可用段均与独立真值一致；真值缺失段已单独标记无法核对。"
            "未发现需要按首个不一致段落回溯的帧级证据。"
        )
        lines.append("")
    else:
        verification = first_bad["verification"]
        lines.append(
            f"首个不一致段：`{first_bad['segment_id']}`；"
            f"问题类型 `{verification.get('first_issue_kind')}`；"
            f"锚点样本 {verification.get('first_issue_anchor_sample')}。"
        )
        lines.append("")
        lines.extend(_frame_evidence_table(verification.get("frame_evidence", [])))
        lines.append("")

    lines.append("## 7. 验收清单")
    lines.append("")
    lines.append("| 检查 | 结果 | 说明 |")
    lines.append("|---|:---:|---|")
    for item in acceptance["checks"]:
        lines.append(
            f"| `{item['check']}` | {'通过' if item['pass'] else '未通过'} | {item['detail']} |"
        )
    lines.append("")
    lines.append(
        f"逐类型真值统计："
        + "；".join(
            f"{DAMAGE_LABELS[damage_type]} 真值 {stats['truth_count']} / "
            f"检测 {stats['detected_count']} / 匹配 {stats['matched_count']} / "
            f"漏报 {stats['false_negative_count']} / 误报 {stats['false_positive_count']} / "
            f"分级一致 {stats['grade_matches']}/{stats['grade_comparisons']}"
            for damage_type, stats in acceptance["type_stats"].items()
        )
    )
    lines.append("")

    lines.append("## 8. 修复工单摘录")
    lines.append("")
    lines.append("| 录音段 | 类型 | 区间（样本） | 分级 | 建议流程 | 人工复核 |")
    lines.append("|---|---|---|---|---|---|")
    for recording in primary["recordings"]:
        for segment in recording["segments"]:
            has_review = bool(segment.get("manual_review"))
            for damage_type in ("clipping", "dc_offset", "noise", "impulse"):
                for item in segment["damage_intervals"][damage_type]:
                    grade_value = item.get("grade") or segment["segment_grades"][damage_type]
                    lines.append(
                        f"| `{segment['segment_id']}` | {DAMAGE_LABELS[damage_type]} | "
                        f"{item['start_sample']}–{item['end_sample']} | {_grade(grade_value)} | "
                        f"{WORKFLOW_LABELS[segment['workflow']['code']]} | "
                        f"{'是' if has_review else '否'} |"
                    )
    lines.append("")
    lines.append(
        "说明：修复前应先按人工复核标记核对临界分级；削波/脉冲帧不参与自适应底噪更新；"
        "真值缺失段仅能用于检测参考，不能用于验收统计。"
    )
    lines.append("")
    return "\n".join(lines)
