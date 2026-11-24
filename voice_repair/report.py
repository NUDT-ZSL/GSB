from __future__ import annotations

import json
from typing import Any


def deterministic_report_view(report: dict[str, Any]) -> dict[str, Any]:
    """Remove run-specific fields before serial/parallel comparison."""
    recordings = []
    for recording in sorted(report.get("recordings", []), key=lambda item: item["recording_id"]):
        recordings.append(
            {
                "recording_id": recording["recording_id"],
                "sample_rate": recording.get("sample_rate"),
                "sample_count": recording.get("sample_count"),
                "input_sha256": recording.get("input_sha256"),
                "status": recording.get("status"),
                "output_sha256": recording.get("output_sha256"),
                "stages": [
                    {
                        "stage": stage["stage"],
                        "name": stage["name"],
                        "total_blocks": stage["total_blocks"],
                        "completed_blocks": stage["completed_blocks"],
                        "complete": stage["complete"],
                        "block_sha256": stage["block_sha256"],
                    }
                    for stage in recording.get("stages", [])
                ],
                "spectral_overlap_max_abs_error": recording.get(
                    "spectral_overlap_max_abs_error"
                ),
                "normalization_gain": recording.get("normalization_gain"),
            }
        )
    issues = sorted(
        (
            {
                "recording_id": issue["recording_id"],
                "code": issue["code"],
                "message": issue["message"],
            }
            for issue in report.get("business_codes", [])
        ),
        key=lambda item: (
            item["recording_id"],
            item["code"],
            item["message"],
        ),
    )
    return {
        "task_id": report.get("task_id"),
        "task_sha256": report.get("task_sha256"),
        "business_codes": issues,
        "recordings": recordings,
        "scheduling": report.get("scheduling"),
        "summary": {
            "total_blocks": report.get("summary", {}).get("total_blocks"),
            "completed_blocks": report.get("summary", {}).get("completed_blocks"),
            "deferred_blocks": report.get("summary", {}).get("deferred_blocks"),
            "rounds": report.get("summary", {}).get("rounds"),
        },
    }


def report_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
