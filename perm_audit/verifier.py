from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .codecs import CANONICAL_MASK


def read_truth(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        value = json.loads(text)
        return value.get("records", value) if isinstance(value, dict) else value
    rows = []
    for line in text.splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def verify_against_truth(
    truth_path: str | Path,
    report_path: str | Path | dict[str, Any],
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    truth_rows = read_truth(truth_path)
    if isinstance(report_path, dict):
        report = report_path
    else:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    decisions = {item["sync_marker"]: item for item in report.get("decisions", [])}

    results: list[dict[str, Any]] = []
    counts = {
        "total": len(truth_rows),
        "passed": 0,
        "failed": 0,
        "exact_mask_matches": 0,
        "conservative_uncertain": 0,
    }

    for truth in sorted(truth_rows, key=lambda row: row["sync_marker"]):
        marker = truth["sync_marker"]
        decision = decisions.get(marker)
        failures: list[str] = []
        if decision is None:
            results.append(
                {
                    "sync_marker": marker,
                    "passed": False,
                    "failures": ["E_TRUTH_MARKER_MISSING_IN_REPORT"],
                }
            )
            counts["failed"] += 1
            continue

        expected_mask = int(truth["expected_effective_mask"]) & CANONICAL_MASK
        observed_mask = int(decision.get("effective_mask", 0)) & CANONICAL_MASK
        error_mask = int(decision.get("error_mask", decision.get("error_bound", 0))) & CANONICAL_MASK
        stable_mismatch = (expected_mask ^ observed_mask) & ~error_mask & CANONICAL_MASK
        exact_match = expected_mask == observed_mask
        if exact_match:
            counts["exact_mask_matches"] += 1
        if stable_mismatch:
            failures.append(f"E_TRUTH_STABLE_MASK_MISMATCH:{stable_mismatch:#06x}")

        truth_hex = truth.get("original_path_bytes_hex", "")
        report_hex = decision.get("original_path_bytes_hex", "")
        if truth_hex != report_hex:
            failures.append("E_TRUTH_PATH_BYTES_MISMATCH")

        requested_bit = int(truth.get("requested_weight") or (1 << int(truth["requested_bit"])))
        ground_decision = truth.get("expected_ground_truth_decision")
        if ground_decision is None:
            ground_decision = "allow" if expected_mask & requested_bit else "deny"
        audit_decision = decision["decision"]
        uncertainty_list = decision.get("uncertain_bits", [])
        uncertain_requested = any(item.get("weight") == requested_bit for item in uncertainty_list)
        if audit_decision == "uncertain":
            counts["conservative_uncertain"] += 1
            if not uncertain_requested and not (error_mask & requested_bit):
                failures.append("E_UNCERTAIN_DECISION_WITHOUT_ERROR_BOUND")
        elif audit_decision not in {"allow", "deny"}:
            failures.append(f"E_UNKNOWN_DECISION:{audit_decision}")
        elif audit_decision != ground_decision:
            failures.append(
                f"E_TRUTH_DECISION_MISMATCH:audit={audit_decision},truth={ground_decision}"
            )

        passed = not failures
        counts["passed" if passed else "failed"] += 1
        results.append(
            {
                "sync_marker": marker,
                "passed": passed,
                "failures": failures,
                "audit_decision": audit_decision,
                "ground_truth_decision": ground_decision,
                "expected_effective_mask": expected_mask,
                "observed_effective_mask": observed_mask,
                "error_mask": error_mask,
                "exact_mask_match": exact_match,
                "stable_mismatch_mask": stable_mismatch,
                "hit_ace_keys": decision.get("hit_ace_keys", []),
                "cumulative_error_bound": decision.get("error_bound"),
                "cumulative_error_bound_sum": decision.get("error_bound_sum"),
                "uncertain_bits": decision.get("uncertain_bits", []),
            }
        )

    verification = {
        "verification_version": "truth-sync-1",
        "ok": counts["failed"] == 0,
        "summary": counts,
        "results": results,
    }
    if output_path is not None:
        atomic_write_json(output_path, verification)
    return verification
