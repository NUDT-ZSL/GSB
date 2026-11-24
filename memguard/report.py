"""Deterministic diagnostic report construction."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .codes import (
    DEGRADATION_CODES,
    OK,
    POLICY_VERSION,
    REPORT_SCHEMA_VERSION,
)
from .models import ArenaRun, Manifest, Rejection, Verdict
from .scheduler import ScheduleConfig
from .util import canonical_digest

STATUS_ORDER = {"no_data": 0, "ok": 1, "out_of_bounds": 2, "error": 3}
CODE_ORDER = {
    OK: 0,
    "OOB_ZERO_LENGTH": 10,
    "OOB_ENDPOINT_TOUCH": 11,
    "OOB_ANCESTOR_CONTAINED": 12,
    "OOB_CROSS_ANCESTOR": 13,
    "OOB_OUTSIDE_ANCESTORS": 14,
}


def _verdict_to_dict(verdict: Verdict) -> dict[str, Any]:
    return {
        "trace_id": verdict.trace_id,
        "access_seq": verdict.access_seq,
        "region_id": verdict.region_id,
        "arena_id": verdict.arena_id,
        "access_kind": verdict.access_kind,
        "status": verdict.status,
        "business_code": verdict.business_code,
        "access_end": verdict.access_end,
        "source_file": verdict.source_file,
        "line_number": verdict.line_number,
        "evidence": dict(verdict.evidence),
    }


def _direction_summary(
    verdicts: Sequence[Verdict], ledger_row: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    statuses = [verdict.status for verdict in verdicts]
    if "error" in statuses:
        status = "error"
    elif "out_of_bounds" in statuses:
        status = "out_of_bounds"
    elif verdicts:
        status = "ok"
    else:
        status = "no_data"
    codes = sorted({verdict.business_code for verdict in verdicts}, key=_code_key)
    primary = (
        sorted(codes, key=_code_key)[0]
        if codes
        else (
            "NO_DATA"
            if ledger_row is None
            else OK
        )
    )
    evidence = [
        {
            "access_seq": verdict.access_seq,
            "trace_id": verdict.trace_id,
            "business_code": verdict.business_code,
            "evidence": dict(verdict.evidence),
        }
        for verdict in sorted(verdicts, key=lambda item: item.stable_key)
    ]
    return {
        "status": status,
        "primary_business_code": primary,
        "business_codes": codes,
        "counts": {
            "total": len(verdicts),
            "ok": statuses.count("ok"),
            "out_of_bounds": statuses.count("out_of_bounds"),
            "error": statuses.count("error"),
        },
        "evidence": evidence,
        "ledger": (
            None
            if ledger_row is None
            else {
                "access_count": int(ledger_row["access_count"]),
                "oob_count": int(ledger_row["oob_count"]),
                "oob_upper_bound": (
                    None
                    if ledger_row["oob_upper_bound"] is None
                    else int(ledger_row["oob_upper_bound"])
                ),
                "processed_access_seq": int(ledger_row["processed_access_seq"]),
            }
        ),
    }


def _code_key(code: str) -> tuple[int, str]:
    return (CODE_ORDER.get(code, 5 if code.startswith("E_") else 6), code)


def build_report(
    *,
    manifest: Manifest,
    arena_runs: Sequence[ArenaRun],
    scheduler_rejections: Sequence[Rejection],
    verdicts: Sequence[Verdict],
    rejections: Sequence[Rejection],
    sequence_diagnostics: Sequence[Mapping[str, Any]],
    topology_diagnostics: Sequence[Mapping[str, Any]],
    schedule_config: ScheduleConfig,
    ledger_rows: Sequence[Mapping[str, Any]],
    checkpoint_metadata: Mapping[str, Any],
    recovery: Mapping[str, Any],
    truth_comparison: Mapping[str, Any],
    batch_count: int,
    skipped_batches: int,
) -> dict[str, Any]:
    input_rejections = sorted(list(rejections), key=lambda item: item.sort_key)
    scheduler_diagnostics = sorted(
        list(scheduler_rejections), key=lambda item: item.sort_key
    )
    verdicts_sorted = sorted(verdicts, key=lambda item: item.stable_key)
    ledger_by_key = {
        (str(row["region_id"]), str(row["access_kind"])): row for row in ledger_rows
    }
    verdicts_by_region_kind: dict[tuple[str, str], list[Verdict]] = {}
    for verdict in verdicts_sorted:
        verdicts_by_region_kind.setdefault(
            (verdict.region_id, verdict.access_kind), []
        ).append(verdict)

    regions: list[dict[str, Any]] = []
    for region_id in sorted(manifest.regions):
        region = manifest.regions[region_id]
        regions.append(
            {
                "region_id": region.region_id,
                "arena_id": region.arena_id,
                "arena_size": region.arena_size,
                "interval": [region.start_offset, region.end_offset],
                "lifetime": [region.lifetime_start_ms, region.lifetime_end_ms],
                "generation": region.generation,
                "parent_region_id": region.parent_region_id,
                "directions": {
                    kind: _direction_summary(
                        verdicts_by_region_kind.get((region_id, kind), []),
                        ledger_by_key.get((region_id, kind)),
                    )
                    for kind in ("read", "write")
                },
            }
        )

    degradation_codes = sorted(
        {
            code
            for run in arena_runs
            for code in run.degradation_codes
            if code in DEGRADATION_CODES
        }
    )
    arenas = [
        {
            "arena_id": run.arena_id,
            "loaded_bytes": run.loaded_bytes,
            "elapsed_ms": round(run.elapsed_ms, 6),
            "degradation_codes": list(run.degradation_codes),
            "budget_degraded": "E_BUDGET" in run.degradation_codes,
            "timed_out": "E_TIMEOUT" in run.degradation_codes,
            "queue_degraded": "E_QUEUE_FULL" in run.degradation_codes,
            "exceeded_budget_bytes": max(
                0, run.loaded_bytes - schedule_config.max_inflight_bytes
            ),
            "timeout_limit_ms": schedule_config.arena_timeout_ms,
            "access_count": len(run.verdicts),
        }
        for run in sorted(arena_runs, key=lambda item: item.arena_id)
    ]

    truth_ok = bool(truth_comparison.get("ok")) or not bool(
        truth_comparison.get("checked")
    )
    semantic_partial = (
        any(verdict.status == "error" for verdict in verdicts_sorted)
        or bool(topology_diagnostics)
        or any(
            str(item.get("business_code")) == "E_ACCESS_SEQ_GAP"
            for item in sequence_diagnostics
        )
    )
    partial = (
        bool(input_rejections)
        or bool(degradation_codes)
        or not truth_ok
        or semantic_partial
    )
    findings = (
        any(verdict.status in {"out_of_bounds", "error"} for verdict in verdicts_sorted)
        or bool(sequence_diagnostics)
        or bool(topology_diagnostics)
    )
    if partial:
        batch_status = "partial"
    elif findings:
        batch_status = "violations"
    else:
        batch_status = "ok"

    business_codes = sorted(
        {
            item.business_code for item in input_rejections
        }
        | {item.business_code for item in scheduler_diagnostics}
        | {verdict.business_code for verdict in verdicts_sorted}
        | set(degradation_codes)
        | {str(item["business_code"]) for item in sequence_diagnostics}
        | {str(item["business_code"]) for item in topology_diagnostics}
        | (
            {str(truth_comparison["business_code"])}
            if truth_comparison.get("business_code")
            else set()
        )
    )
    access_values = [verdict.access_seq for verdict in verdicts_sorted]
    access_watermark = max(access_values) if access_values else -1
    counts = {
        "total": len(verdicts_sorted),
        "ok": sum(verdict.status == "ok" for verdict in verdicts_sorted),
        "out_of_bounds": sum(
            verdict.status == "out_of_bounds" for verdict in verdicts_sorted
        ),
        "error": sum(verdict.status == "error" for verdict in verdicts_sorted),
    }

    region_documents = regions
    access_documents = [_verdict_to_dict(verdict) for verdict in verdicts_sorted]
    rejection_documents = [item.to_dict() for item in input_rejections]
    sequence_documents = sorted(
        (dict(item) for item in sequence_diagnostics),
        key=lambda item: (
            str(item.get("business_code", "")),
            int(item.get("access_seq", -1)),
        ),
    )
    topology_documents = [dict(item) for item in topology_diagnostics]
    decision_digest = canonical_digest(
        {
            "accesses": access_documents,
            "regions": region_documents,
            "rejections": rejection_documents,
            "sequence_diagnostics": sequence_documents,
            "topology_diagnostics": topology_documents,
            "ledger_rows": [dict(row) for row in ledger_rows],
        }
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "batch": {
            "status": batch_status,
            "conclusion": batch_status,
            "business_codes": business_codes,
            "layout_key": manifest.layout_key,
            "region_layout_digest": manifest.region_layout_digest,
            "trace_input_digest": manifest.trace_input_digest,
            "region_files": list(manifest.region_files),
            "trace_files": list(manifest.trace_files),
            "access_seq_watermark": access_watermark,
            "access_counts": counts,
            "batch_count": batch_count,
            "skipped_batches": skipped_batches,
            "decision_digest": decision_digest,
        },
        "scheduler": {
            "config": {
                "max_workers": schedule_config.max_workers,
                "max_inflight_bytes": schedule_config.max_inflight_bytes,
                "arena_timeout_ms": schedule_config.arena_timeout_ms,
                "max_pending": schedule_config.max_pending,
            },
            "arenas": arenas,
            "degradation_codes": degradation_codes,
            "degraded": bool(degradation_codes),
            "diagnostics": [item.to_dict() for item in scheduler_diagnostics],
        },
        "regions": region_documents,
        "accesses": access_documents,
        "rejections": rejection_documents,
        "sequence_diagnostics": sequence_documents,
        "topology_diagnostics": topology_documents,
        "ledger": {
            "rows": [dict(row) for row in ledger_rows],
            "checkpoint": dict(checkpoint_metadata),
        },
        "recovery": dict(recovery),
        "truth": dict(truth_comparison),
    }


def report_exit_code(report: Mapping[str, Any]) -> int:
    return 1 if report.get("batch", {}).get("status") == "partial" else 0
