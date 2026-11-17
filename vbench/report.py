"""运行诊断 JSON 报告汇总。"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from . import denoise as dn
from .config import Config
from .errors import Failure
from .pipeline import ClipResult
from .scheduler import SchedulingDecision


def build_report(config: Config, results: List[ClipResult],
                 failures: List[Failure], decision: SchedulingDecision,
                 manifest_version: int,
                 extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    tier_stats: Dict[str, Dict[str, Any]] = {}
    segment_judgement: List[Dict[str, Any]] = []
    reuse_records: List[Dict[str, Any]] = []

    for result in results:
        for tier_name, frames in result.decoded.items():
            stat = tier_stats.setdefault(tier_name, {
                "frame_count": 0, "clip_count": 0,
                "max_channel_diff": 0})
            ordered = [frames[index] for index in sorted(frames)]
            stat["frame_count"] += len(ordered)
            stat["clip_count"] += 1
            stack = np.stack(ordered, axis=0)
            if stack.shape[0] > 1:
                diff = int(np.abs(
                    stack[1:].astype(np.int16) - stack[:-1].astype(np.int16)
                ).max())
                stat["max_channel_diff"] = max(stat["max_channel_diff"], diff)
        segment_judgement.append({
            "clip_id": result.clip_id,
            "divergent_steps": result.segments["divergent_steps"],
            "shared_steps": result.segments["shared_steps"],
            "divergence_failures": [item.to_dict()
                                    for item in result.divergence],
            "shared_ok": not any(
                item.code == "E_PATH_DIVERGENCE"
                for item in result.divergence),
            "stable_task_key_count": len(result.task_keys),
            "stable_task_keys_sorted": result.task_keys == sorted(result.task_keys),
        })
        for record in result.tier_reuse:
            reuse_records.append({"clip_id": result.clip_id, **record})

    budget = {
        "step_budget_total": decision.step_budget_total,
        "step_budget_used": decision.step_budget_used,
        "step_budget_remaining": decision.step_budget_total
        - decision.step_budget_used,
        "pending_tier_count": len(decision.pending_tiers),
        "memory_peak_mb": round(decision.memory_peak_mb, 3),
        "memory_cap_mb": config.memory_cap_mb,
    }
    report = {
        "schema_version": 1,
        "config_hash": config.config_hash(),
        "tiers": [tier.name for tier in config.tiers],
        "clip_count": len(results),
        "tier_frame_stats": tier_stats,
        "shared_segment_judgement": segment_judgement,
        "tier_reuse_records": reuse_records,
        "budget_consumption": budget,
        "decision_record": decision.to_dict(),
        "snapshot_version": manifest_version,
        "failure_list": [item.to_dict() for item in failures],
        "failure_summary": _summarize(failures),
    }
    if extra:
        report.update(extra)
    return report


def _summarize(failures: List[Failure]) -> Dict[str, int]:
    summary: Dict[str, int] = {}
    for item in failures:
        summary[item.code] = summary.get(item.code, 0) + 1
    return summary
