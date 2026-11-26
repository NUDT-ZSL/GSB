"""Deterministic report rendering helpers."""

from __future__ import annotations

from typing import Any, Dict


def render_markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        "# 语音情感与话轮诊断报告",
        "",
        f"- Dialogue: `{report.get('dialogue_id')}`",
        f"- Seed: `{report.get('seed')}`",
        f"- Frames: `{report.get('frame_count')}`",
        f"- PCM SHA256: `{report.get('pcm_sha256')}`",
        "",
        "## 阶段与恢复",
        "",
    ]
    for stage in report.get("stages", []):
        checksum = stage.get("content_checksum")
        checksum_text = f"{checksum[:16]}..." if checksum else "see snapshot file"
        lines.append(
            f"- `{stage['stage']}` version={stage['version']} "
            f"ranges={stage['completed_ranges']} checksum=`{checksum_text}`"
        )

    anomaly = report.get("anomalies", {})
    lines.extend(
        [
            "",
            "## 话轮异常",
            "",
            f"- status: `{anomaly.get('status')}`",
            f"- count: `{anomaly.get('anomaly_count')}`",
        ]
    )
    for event in anomaly.get("events", []):
        lines.append(
            f"- `{event['event_id']}` {event['type']} "
            f"frames=[{event['start_frame']},{event['end_frame']}) "
            f"confidence={event['confidence']}"
        )

    migration = report.get("migration", {})
    lines.extend(
        [
            "",
            "## 账本迁移",
            "",
            f"- v1/v2 semantic equivalent: `{migration.get('cross_version_equivalent')}`",
            f"- completion records: `{migration.get('completion_count')}`",
            f"- downgrade discard records: `{migration.get('discard_count')}`",
            f"- roundtrip equivalent: `{migration.get('roundtrip_equivalent')}`",
            "",
            "## 调度预算",
            "",
            f"- used process pool: `{report.get('scheduling', {}).get('used_process_pool')}`",
            f"- workers: `{report.get('scheduling', {}).get('worker_count')}`",
            f"- batches: `{report.get('scheduling', {}).get('admission', {}).get('batch_count')}`",
            f"- planned transitions: `{report.get('scheduling', {}).get('admission', {}).get('planned_transition_count')}`",
            f"- resident feature bytes: `{report.get('scheduling', {}).get('admission', {}).get('resident_feature_bytes')}`",
            "",
        ]
    )
    return "\n".join(lines)
