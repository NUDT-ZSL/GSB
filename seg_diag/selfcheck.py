"""Cross-module consistency checks with stable E_INCONSISTENT reporting."""

from __future__ import annotations

from typing import Any

import numpy as np

from .codes import CODES
from .errors import DiagnosticError
from .ledger import build_ledger, verify_ledger_checksum
from .models import UnifiedConfig
from .normalization import normalize_v1, normalize_v2


def _inconsistent(message: str, **details: Any) -> DiagnosticError:
    details.setdefault("business_code", CODES.INCONSISTENT)
    return DiagnosticError(CODES.INCONSISTENT, message, details)


def first_array_mismatch(actual: np.ndarray, expected: np.ndarray) -> dict[str, Any] | None:
    actual = np.asarray(actual)
    expected = np.asarray(expected)
    if actual.shape != expected.shape:
        return {
            "shape_mismatch": True,
            "actual_shape": list(actual.shape),
            "expected_shape": list(expected.shape),
        }
    mismatch = actual != expected
    if not np.any(mismatch):
        return None
    y, x = (int(v) for v in np.unravel_index(int(np.argmax(mismatch)), mismatch.shape))
    return {
        "y": y,
        "x": x,
        "location": f"P:{y}:{x}",
        "actual": _scalar(actual[y, x]),
        "expected": _scalar(expected[y, x]),
        "business_code": CODES.INCONSISTENT,
    }


def _scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    return value


def verify_v1_upgrade_contract(
    v1_instance: np.ndarray, original_v1: UnifiedConfig, upgraded_v2_style: UnifiedConfig
) -> dict[str, Any]:
    before = normalize_v1(v1_instance, original_v1)
    after = normalize_v1(v1_instance, upgraded_v2_style)
    for field in ["category", "instance", "accepted"]:
        mismatch = first_array_mismatch(after[field], before[field])
        if mismatch is not None:
            raise _inconsistent(
                "v1 -> v2 migration changes normalized pixel contract",
                field=field,
                first_mismatch=mismatch,
            )
    return {"direction": "v1_to_v2", "pixel_contract_consistent": True}


def verify_v2_downgrade_contract(
    arrays: dict[str, np.ndarray],
    original_v2: UnifiedConfig,
    downgraded_v1: UnifiedConfig,
    *,
    policy: str = "discard",
) -> dict[str, Any]:
    original = normalize_v2(
        arrays["v2_category"], arrays["v2_instance"], arrays["v2_confidence"], original_v2
    )
    frozen_v1_instance = np.where(original["accepted"], original["instance"], 0).astype(np.uint16)
    downgraded = normalize_v1(frozen_v1_instance, downgraded_v1)
    for field, expected in [
        ("category", original["category"]),
        ("instance", original["instance"]),
        ("accepted", original["accepted"]),
    ]:
        mismatch = first_array_mismatch(downgraded[field], expected)
        if mismatch is not None:
            raise _inconsistent(
                "v2 -> v1 migration changes frozen threshold-adjudicated pixel contract",
                field=field,
                policy=policy,
                first_mismatch=mismatch,
            )
    return {
        "direction": "v2_to_v1",
        "policy": policy,
        "pixel_contract_consistent": True,
        "frozen_accepted_pixel_count": int(original["accepted"].sum()),
    }


def verify_independent_ledger(
    arrays: dict[str, np.ndarray],
    v1_config: UnifiedConfig,
    v2_config: UnifiedConfig,
    ledger: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    recomputed = build_ledger(
        arrays,
        v1_config,
        v2_config,
        seed=seed,
        threshold_strategy=ledger.get("threshold_strategy", "strict"),
        tolerance=float(ledger.get("tolerance", 1e-12)),
        region_size=int(ledger.get("region_size", 4)),
        ledger_version=int(ledger.get("ledger_version", 1)),
    )
    embedded_checksum_valid = verify_ledger_checksum(ledger)
    if not embedded_checksum_valid or recomputed["checksum"] != ledger.get("checksum"):
        first_mismatch = _first_ledger_row_mismatch(ledger["rows"], recomputed["rows"])
        raise _inconsistent(
            "ledger cannot be independently recomputed from immutable inputs and configs",
            expected_checksum=recomputed["checksum"],
            actual_checksum=ledger.get("checksum"),
            embedded_checksum_valid=embedded_checksum_valid,
            first_mismatch=first_mismatch,
        )
    return {
        "independent_recompute_consistent": True,
        "row_count": len(ledger["rows"]),
        "checksum": ledger["checksum"],
    }


def _first_ledger_row_mismatch(actual_rows: list[dict[str, Any]], expected_rows: list[dict[str, Any]]) -> dict[str, Any]:
    actual_by_id = {row["row_id"]: row for row in actual_rows}
    for expected in expected_rows:
        actual = actual_by_id.get(expected["row_id"])
        if actual is None:
            return {"row_id": expected["row_id"], "missing": True}
        if actual.get("winner") != expected.get("winner"):
            return {
                "row_id": expected["row_id"],
                "location": expected["row_id"].replace("P:", "P/"),
                "actual_winner": actual.get("winner"),
                "expected_winner": expected.get("winner"),
                "business_code": CODES.INCONSISTENT,
            }
        if actual.get("dependency_key") != expected.get("dependency_key"):
            return {"row_id": expected["row_id"], "dependency_key_mismatch": True}
    return {"location": "unknown"}


def verify_dependency_coverage(plan: dict[str, Any], incremental_ledger: dict[str, Any] | None = None) -> dict[str, Any]:
    coverage = plan["coverage_check"]
    if not coverage["disjoint"] or not coverage["union_all_rows"]:
        raise _inconsistent("recompute/reuse coverage check failed", coverage=coverage)
    if coverage["full_image_recompute"]:
        raise _inconsistent(
            "targeted revision degenerated into full-image recomputation", coverage=coverage
        )
    result = {
        "dependency_sets_consistent": True,
        "recompute_count": coverage["recompute_count"],
        "reuse_count": coverage["reuse_count"],
        "disjoint": coverage["disjoint"],
        "union_all_rows": coverage["union_all_rows"],
    }
    if incremental_ledger is not None:
        actual_computed = set(incremental_ledger.get("_computed_rows", []))
        expected = set(plan["recompute_set"])
        if actual_computed != expected:
            missing = sorted(expected - actual_computed)
            extra = sorted(actual_computed - expected)
            raise _inconsistent(
                "actual recomputed rows do not match dependency plan",
                missing_rows=missing[:10],
                extra_rows=extra[:10],
                first_missing=missing[0] if missing else None,
            )
        result["actual_computed_matches_plan"] = True
        result["cache_reused_rows"] = len(incremental_ledger.get("_reused_rows", []))
    return result


def verify_checkpoint(checkpoint: dict[str, Any], *, expected_region_count: int | None = None) -> dict[str, Any]:
    required = ["completed_regions", "ledger_version", "ledger_checksum"]
    for field in required:
        if field not in checkpoint:
            raise _inconsistent("checkpoint missing required field", field=field)
    if expected_region_count is not None and len(checkpoint["completed_regions"]) != expected_region_count:
        raise _inconsistent(
            "checkpoint completed region count mismatch",
            expected=expected_region_count,
            actual=len(checkpoint["completed_regions"]),
        )
    return {
        "checkpoint_consistent": True,
        "completed_region_count": len(checkpoint["completed_regions"]),
        "ledger_version": checkpoint["ledger_version"],
        "ledger_checksum": checkpoint["ledger_checksum"],
    }


def run_self_check(
    *,
    arrays: dict[str, np.ndarray],
    v1_config: UnifiedConfig,
    v2_config: UnifiedConfig,
    ledger: dict[str, Any],
    seed: int,
    upgraded_v2_config: UnifiedConfig | None = None,
    downgraded_v1_config: UnifiedConfig | None = None,
    downgrade_policy: str = "discard",
    migration_v2_config: UnifiedConfig | None = None,
    dependency_plan: dict[str, Any] | None = None,
    incremental_ledger: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    expected_region_count: int | None = None,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    upgraded = upgraded_v2_config or v1_config
    checks["v1_to_v2_contract"] = verify_v1_upgrade_contract(
        arrays["v1_instance"], v1_config, upgraded
    )
    if downgraded_v1_config is not None:
        checks["v2_to_v1_contract"] = verify_v2_downgrade_contract(
            arrays,
            migration_v2_config or v2_config,
            downgraded_v1_config,
            policy=downgrade_policy,
        )
    checks["independent_ledger"] = verify_independent_ledger(
        arrays, v1_config, v2_config, ledger, seed=seed
    )
    if dependency_plan is not None:
        checks["dependency_coverage"] = verify_dependency_coverage(
            dependency_plan, incremental_ledger
        )
    if checkpoint is not None:
        checks["checkpoint"] = verify_checkpoint(
            checkpoint, expected_region_count=expected_region_count
        )
    return {"business_code": CODES.OK, "ok": True, "checks": checks}
