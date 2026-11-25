from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .groundtruth import ParsedTruth
from .ledger import AdjudicatedLedger
from .parsers.formats import parse_rle, parse_v1, parse_v2
from .probes import Probe

Timer = Callable[[], int]


def fallback_reparse(probe: Probe, truths: dict[str, ParsedTruth]) -> dict[str, Any]:
    """Reload and validate source blocks, then reconstruct one pixel trace."""

    reparsed: dict[str, Any] = {}
    for fmt, truth in truths.items():
        if fmt == "v1":
            reparsed_truth = parse_v1(truth.files[0])
        elif fmt == "v2":
            metadata_path = next(
                path for path in truth.files
                if path.endswith(".sqlite3") or path.endswith(".sqlite")
            )
            reparsed_truth = parse_v2(Path(metadata_path).parent)
        elif fmt == "RLE":
            reparsed_truth = parse_rle(truth.files[0])
        else:
            continue
        reparsed[fmt] = reparsed_truth.raw_at(probe.x, probe.y)
    return {"reparsed_formats": sorted(reparsed), "raw": reparsed}


def _business_code(status: str, degraded: bool) -> str:
    if status == "MATCH":
        return "OK_PROBE_CONFIRMED_DEGRADED" if degraded else "OK_PROBE_CONFIRMED"
    if status == "IGNORE":
        return "OK_IGNORE_CONFIRMED_DEGRADED" if degraded else "OK_IGNORE_CONFIRMED"
    return "E_PROBE_MISMATCH_DEGRADED" if degraded else "E_PROBE_MISMATCH"


def execute_probe(
    probe: Probe,
    ledger: AdjudicatedLedger,
    truths: dict[str, ParsedTruth],
    *,
    fast_confidence_threshold: float = 0.75,
    degraded_aggregate: bool = False,
    timer: Timer = time.perf_counter_ns,
) -> dict[str, Any]:
    started = timer()
    pixel = ledger.pixel_at(probe.x, probe.y)
    winner = pixel["winner"]
    v2_confidence = float(truths["v2"].confidence[probe.y, probe.x]) if "v2" in truths else 1.0
    fast_eligible = v2_confidence >= fast_confidence_threshold
    candidate_reparse = not fast_eligible

    fallback_info: dict[str, Any] | None = None
    degradation: dict[str, Any] = {
        "degraded": False,
        "category": None,
        "reason": None,
    }

    if degraded_aggregate and candidate_reparse:
        speed_class = "degraded_aggregate"
        source_resolved = False
        active_sources = ["AGGREGATE_LEDGER"]
        degradation = {
            "degraded": True,
            "category": "AGGREGATE_SOURCE_UNRESOLVED",
            "reason": "slow-probe budget share exceeded threshold; fallback source attribution was skipped",
        }
    elif candidate_reparse:
        fallback_info = fallback_reparse(probe, truths)
        speed_class = "slow"
        source_resolved = True
        active_sources = list(pixel["sources_hit"])
    else:
        speed_class = "fast"
        source_resolved = True
        active_sources = list(pixel["sources_hit"])

    elapsed_ns = max(0, timer() - started)
    probe_budget_ns = int(round(probe.time_budget_ms * 1_000_000))
    budget_overrun = elapsed_ns > probe_budget_ns

    actual_ignore = bool(winner["ignore"])
    if probe.expected_ignore and actual_ignore:
        status = "IGNORE"
    elif (
        not probe.expected_ignore
        and not actual_ignore
        and int(winner["category"]) == probe.expected_category
        and int(winner["instance"]) == probe.expected_instance
    ):
        status = "MATCH"
    else:
        status = "MISMATCH"

    record = {
        "uid": probe.uid,
        "sequence": probe.sequence,
        "probe_id": probe.probe_id,
        "plan_index": probe.plan_index,
        "coordinate": probe.coordinate(),
        "expected": {
            "category": probe.expected_category,
            "instance": probe.expected_instance,
            "ignore": probe.expected_ignore,
        },
        "priority": probe.priority,
        "time_budget_ms": probe.time_budget_ms,
        "candidate_reparse": candidate_reparse,
        "execution": {
            "path": speed_class,
            "speed_class": speed_class,
            "format_sources_resolved": source_resolved,
            "sources_hit": active_sources,
            "fallback": fallback_info,
        },
        "ledger_result": {
            "category": int(winner["category"]),
            "instance": int(winner["instance"]),
            "ignore": actual_ignore,
            "adjudicated_sources_hit": list(pixel["sources_hit"]),
            "weighted_confidence": pixel["weighted_confidence"],
            "ledger_content_checksum": ledger.content_checksum,
        },
        "timing": {
            "elapsed_ns": elapsed_ns,
            "charged_ns": elapsed_ns,
            "probe_budget_ns": probe_budget_ns,
            "budget_overrun": budget_overrun,
            "timer": "time.perf_counter_ns" if timer is time.perf_counter_ns else "injected_fake_timer",
        },
        "degradation": degradation,
        "status": status,
        "business_code": _business_code(status, degradation["degraded"]),
        "mismatch": None if status != "MISMATCH" else {
            "expected": {
                "category": probe.expected_category,
                "instance": probe.expected_instance,
                "ignore": probe.expected_ignore,
            },
            "actual": {
                "category": int(winner["category"]),
                "instance": int(winner["instance"]),
                "ignore": actual_ignore,
            },
        },
    }
    return record


def evaluate_degradation(
    records: list[dict[str, Any]],
    *,
    total_budget_ns: int | None,
    threshold: float,
) -> dict[str, Any]:
    total_spent_ns = sum(int(record["timing"]["charged_ns"]) for record in records)
    slow_spent_ns = sum(
        int(record["timing"]["charged_ns"])
        for record in records
        if record["execution"]["speed_class"] == "slow"
    )
    slow_count = sum(1 for record in records if record["execution"]["speed_class"] == "slow")
    slow_cost_ratio = slow_spent_ns / total_spent_ns if total_spent_ns else 0.0
    slow_to_budget_ratio = slow_spent_ns / total_budget_ns if total_budget_ns else 0.0
    triggered = slow_count > 0 and slow_cost_ratio > threshold
    return {
        "threshold": threshold,
        "slow_count": slow_count,
        "record_count": len(records),
        "slow_spent_ns": slow_spent_ns,
        "total_spent_ns": total_spent_ns,
        "slow_cost_ratio": slow_cost_ratio,
        "slow_to_total_budget_ratio": slow_to_budget_ratio,
        "triggered": triggered,
        "degradation_category": "AGGREGATE_SOURCE_UNRESOLVED" if triggered else None,
    }
