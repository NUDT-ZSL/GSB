from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .adjudication import AdjudicationResult, decision_digest
from .ledger import Ledger
from .models import NormalizedSample, layout_version
from .util import atomic_write_json


def build_report(
    sample: NormalizedSample,
    result: AdjudicationResult,
    ledger: Ledger,
    *,
    operation: str,
    batch_size: int | None = None,
    recovery: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    checkpoint_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sessions = [decision.to_dict() for decision in result.sessions]
    repair_counts = Counter(item["repair_state"] for item in sessions)
    code_counts: Counter[str] = Counter()
    for item in sessions:
        code_counts.update(item["codes"])
    for rejection in sample.rejections:
        code_counts[rejection.code] += 1
    for conflict in sample.policy_conflicts:
        code_counts["E_POLICY_CONFLICT"] += 1
    for cycle in sample.cycles:
        code_counts["E_SESSION_CYCLE"] += 1

    fixed_sessions = [
        item["session_id"]
        for item in sessions
        if item["repair_state"] == "fixed"
    ]
    vulnerable_sessions = [
        item["session_id"]
        for item in sessions
        if item["repair_state"] in {"vulnerable", "orphaned", "rate_limited"}
    ]
    fixation_sessions = [
        item["session_id"]
        for item in sessions
        if "E_SESSION_FIXATION" in item["codes"]
    ]
    policy_generations = sorted(
        {
            int(item["policy_generation"])
            for item in sessions
            if int(item["policy_generation"]) > 0
        }
    )

    business_codes = set(result.business_codes)
    business_codes.update(rejection.code for rejection in sample.rejections)
    if sample.policy_conflicts:
        business_codes.add("E_POLICY_CONFLICT")
    if sample.cycles:
        business_codes.add("E_SESSION_CYCLE")

    report = {
        "report_version": 1,
        "operation": operation,
        "decision_digest": decision_digest(sample, result),
        "sample": {
            "sample_id": sample.sample_id,
            "seed": sample.manifest.seed,
            "observed_clock_ms": sample.manifest.observed_clock_ms,
            "sample_digest": sample.sample_digest,
            "session_count": len(sample.sessions),
            "event_count": len(sample.events),
        },
        "processing": {
            "processed_events": result.processed_count,
            "total_events": result.total_event_count,
            "decision_watermark": result.decision_watermark,
            "batch_size": batch_size,
        },
        "policy": {
            "layout_version": layout_version(sample.policy_views.values()),
            "views": [
                sample.policy_views[key].to_dict()
                for key in sorted(sample.policy_views)
            ],
            "conflicts": [
                conflict.to_dict() for conflict in sample.policy_conflicts
            ],
        },
        "summary": {
            "repair_state_counts": dict(sorted(repair_counts.items())),
            "business_code_counts": dict(sorted(code_counts.items())),
            "fixed_sessions": sorted(fixed_sessions),
            "vulnerable_sessions": sorted(vulnerable_sessions),
            "session_fixation_sessions": sorted(fixation_sessions),
            "policy_generations": policy_generations,
        },
        "fix_verification": {
            "fixed_sessions": sorted(fixed_sessions),
            "session_fixation_sessions": sorted(fixation_sessions),
            "all_fixed_sessions_rotated": all(
                not item["codes"]
                or "E_SESSION_FIXATION" not in item["codes"]
                for item in sessions
                if item["repair_state"] == "fixed"
            ),
            "repair_path_closed_for_fixed_sessions": bool(fixed_sessions)
            and not any(
                "E_SESSION_FIXATION" in item["codes"]
                for item in sessions
                if item["repair_state"] == "fixed"
            ),
        },
        "sessions": sessions,
        "successor_edges": [edge.to_dict() for edge in result.edges],
        "cycles": [cycle.to_dict() for cycle in sample.cycles],
        "revocations": [item.to_dict() for item in result.revocations],
        "rejections": [
            rejection.to_dict() for rejection in sample.rejections
        ],
        "business_codes": sorted(business_codes),
        "ledger": {
            "digest": ledger.ledger_digest(),
            "checkpoint": checkpoint,
            "recovery": recovery,
            "validation": checkpoint_validation,
        },
    }
    return report


def write_report(path: Path, report: dict[str, Any]) -> str:
    return atomic_write_json(path, report)
