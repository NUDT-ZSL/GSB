"""Structured JSON report construction."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .codes import E_NOT_EQUIVALENT, SCHEMA_VERSION
from .models import (
    CaseConclusion,
    MappingResult,
    SnapshotBundle,
)


def _snapshot_summary(bundle: SnapshotBundle) -> dict[str, Any]:
    return {
        "sample_id": bundle.sample_id,
        "snapshot_id": bundle.snapshot_id,
        "observed_clock_ms": bundle.observed_clock_ms,
        "v1_generation": bundle.v1_generation,
        "v2_generation": bundle.v2_generation,
        "rule_generation": bundle.rule_generation,
        "mapping_generation": bundle.mapping_generation,
        "conflict_policy": bundle.conflict_policy,
        "mapping_direction": bundle.mapping_direction,
        "rule_set_fingerprint": bundle.rule_set_fingerprint(),
        "mapping_fingerprint": bundle.mapping_fingerprint(),
    }


def _collect_codes(
    bundle: SnapshotBundle,
    mapping: MappingResult,
    results: dict[str, CaseConclusion],
) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for rejection in bundle.rejections:
        counter[rejection.code] += 1
    for issue in mapping.issues:
        counter[issue.code] += 1
    for result in results.values():
        for code in (result.code, result.v1.code, result.v2.code, result.unified.code):
            if code:
                counter[code] += 1
    return dict(sorted(counter.items()))


def build_audit_report(
    bundle: SnapshotBundle,
    mapping: MappingResult,
    results: dict[str, CaseConclusion],
    *,
    closure: dict[str, Any],
    mode: str | None,
    recomputed_cases: list[str],
    sequence: int,
    report_type: str = "equivalence",
) -> dict[str, Any]:
    ordered_ids = [case.case_id for case in bundle.cases]
    present = [results[case_id] for case_id in ordered_ids if case_id in results]
    missing = sorted(set(ordered_ids) - set(results))
    case_dicts = [result.to_dict() for result in present]
    equivalent_count = sum(1 for result in present if result.equivalent)
    not_equivalent_count = len(present) - equivalent_count
    closure_value = dict(closure)
    closure_value["mode"] = mode
    closure_value["recomputed_cases"] = list(recomputed_cases)
    closure_value["unaffected_cases"] = sorted(
        set(ordered_ids) - set(recomputed_cases)
    )
    all_equivalent = (
        not missing
        and len(present) == len(ordered_ids)
        and not_equivalent_count == 0
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": report_type,
        "sample_id": bundle.sample_id,
        "snapshot": _snapshot_summary(bundle),
        "ledger_sequence": sequence,
        "overall": {
            "equivalent": all_equivalent,
            "code": None if all_equivalent else E_NOT_EQUIVALENT,
        },
        "summary": {
            "total_cases": len(ordered_ids),
            "concluded_cases": len(present),
            "equivalent_cases": equivalent_count,
            "not_equivalent_cases": not_equivalent_count,
            "missing_results": missing,
        },
        "business_codes": _collect_codes(bundle, mapping, results),
        "mapping_reconciliation": mapping.to_dict(),
        "cases": case_dicts,
        "rejections": [item.to_dict() for item in bundle.rejections],
        "invalidation_closure": closure_value,
    }


def build_import_report(
    bundle: SnapshotBundle,
    mapping: MappingResult,
    *,
    import_result: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    code_counter: Counter[str] = Counter()
    for rejection in bundle.rejections:
        code_counter[rejection.code] += 1
    for issue in mapping.issues:
        code_counter[issue.code] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "report_type": "import",
        "sample_id": bundle.sample_id,
        "snapshot": _snapshot_summary(bundle),
        "ledger_sequence": sequence,
        "replacement": import_result["replacement"],
        "summary": {
            "cases": len(bundle.cases),
            "rules": len(bundle.rules),
            "rejections": len(bundle.rejections),
            "mapping_pairs": len(mapping.pairs),
            "pending_cases": len(import_result["pending_cases"]),
        },
        "business_codes": dict(sorted(code_counter.items())),
        "mapping_reconciliation": mapping.to_dict(),
        "rejections": [item.to_dict() for item in bundle.rejections],
        "invalidation_closure": import_result["closure"],
    }
