from __future__ import annotations

from pathlib import Path
from typing import Any


STAGE_LABELS = {
    "model_validation": "模型包校验",
    "quantization": "逐层量化",
    "calibration": "校准统计",
    "inference": "分块推理",
}


def build_dashboard(
    result: dict[str, Any],
    *,
    result_path: Path | None = None,
    recovery_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = result["diagnostics"]
    progress = result["progress"]
    stages = []
    for stage in ("model_validation", "quantization", "calibration", "inference"):
        entry = progress.get("latest_by_stage", {}).get(stage)
        stages.append(
            {
                "stage": stage,
                "label": STAGE_LABELS[stage],
                "version": entry.get("version") if entry else None,
                "completed_layers": entry.get("completed_layers", []) if entry else [],
                "stage_complete": entry.get("stage_complete", False) if entry else False,
                "checksum": entry.get("checksum") if entry else None,
                "next_layer_index": entry.get("next_layer_index") if entry else None,
            }
        )
    recent_errors = result.get("recent_errors", [])
    last_error = recent_errors[-1] if recent_errors else None
    invalidation = result.get("execution", {}).get("invalidation")
    dashboard = {
        "schema": 1,
        "title": "QuantTrace Lab",
        "subtitle": "低精度量化与分块推理诊断工作台",
        "model_tag": result["model_tag"],
        "manifest_digest": result["manifest_digest"],
        "result_path": str(result_path) if result_path else None,
        "summary": {
            "layer_count": len(result["manifest"]),
            "dominant_source": diagnostics["dominant_source"],
            "consistency_ok": bool(result["consistency"]["ok"]),
            "fixed_point_ok": bool(diagnostics["fixed_point_all_match"]),
            "intervals_legal": bool(diagnostics["intervals_legal"]),
            "final_prediction_checksum": result["inference"][
                "final_prediction_checksum"
            ],
            "comparison_count": result["calibration"]["comparison_count"],
            "total_weight_bytes": result["quantization"]["total_weight_bytes"],
            "latest_version": progress.get("latest", {}).get("version")
            if progress.get("latest")
            else None,
            "last_error_code": last_error.get("code") if last_error else "OK",
        },
        "manifest": result["manifest"],
        "quantization": result["quantization"],
        "calibration": result["calibration"],
        "inference": result["inference"],
        "diagnostics": diagnostics,
        "progress": progress,
        "progress_stages": stages,
        "recovery": result.get("recovery"),
        "recovery_comparison": recovery_comparison,
        "invalidation": invalidation,
        "budget": result["budget"],
        "consistency": result["consistency"],
        "recent_errors": recent_errors,
        "last_error": last_error,
        "config": result["config"],
    }
    return dashboard
