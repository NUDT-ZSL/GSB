from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .canon import canonical_hash, stable_diff
from .engine import (
    DEFAULT_AS_OF_CLOCK_MS,
    DEFAULT_CONCURRENCY,
    DEFAULT_INJECTION_CLOCK_MS,
    DEFAULT_TIME_BUDGET_MS,
    Engine,
    canonical_diagnostic_report,
)
from .errors import Code
from .loader import load_bundle
from .storage import Storage
from .watermark import compute_all_watermarks


def _hash_mismatches(items: List[Dict[str, Any]], kind: str) -> List[Dict[str, Any]]:
    mismatches = []
    for item in items:
        expected_hash = item.get("content_hash")
        actual_hash = canonical_hash({key: value for key, value in item.items() if key != "content_hash"})
        if expected_hash != actual_hash:
            mismatches.append({"kind": kind, "id": item.get("patch_id") or item.get("closure_id") or item.get("run_id"), "expected_hash": expected_hash, "actual_hash": actual_hash})
    return mismatches


def verify_workbench(sample_dir: str | Path, db_path: str | Path) -> Dict[str, Any]:
    sample_path = Path(sample_dir)
    main_db = Path(db_path)
    bundle = load_bundle(sample_path)
    expected = canonical_diagnostic_report(bundle)
    temp_parent = main_db.parent if main_db.parent.exists() else Path("state")
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="verify-", dir=str(temp_parent)))
    try:
        temp_db = temp_dir / "verify.sqlite3"
        engine = Engine(sample_path, temp_db)
        engine.load()
        diagnose_result = engine.diagnose()
        patch_result = engine.patch()
        fresh_status = engine.status()
        diffs: List[Dict[str, Any]] = []
        diffs.extend(stable_diff(expected["partition_watermarks"], fresh_status["partition_watermarks"], "$.partition_watermarks"))
        diffs.extend(stable_diff(expected["subscription_cursors"], fresh_status["subscription_cursors"], "$.subscription_cursors"))
        diffs.extend(stable_diff(expected["patches"], fresh_status["patches"], "$.patches"))
        diffs.extend(stable_diff([expected["diagnostic_run"]], fresh_status["diagnostic_runs"], "$.diagnostic_runs"))
        fresh_status["recovery"]["committed_checkpoint_count"] = 0
        fresh_status["recovery"]["prepared_checkpoint_ids"] = []
        fresh_status["recovery"]["has_half_committed_record"] = False
        hash_mismatches = []
        hash_mismatches.extend(_hash_mismatches(fresh_status["patches"], "patch"))
        hash_mismatches.extend(_hash_mismatches(fresh_status["invalidation_event_chains"], "closure"))
        hash_mismatches.extend(_hash_mismatches(fresh_status["diagnostic_runs"], "diagnostic_run"))
        current = _verify_current_database(main_db, bundle)
        ok = not diffs and not hash_mismatches and current["ok"]
        return {
            "ok": ok,
            "code": Code.OK if ok else Code.VERIFY_MISMATCH,
            "sample_dir": str(sample_path),
            "fresh_database": str(temp_db),
            "defaults": {
                "injection_clock_ms": DEFAULT_INJECTION_CLOCK_MS,
                "as_of_clock_ms": DEFAULT_AS_OF_CLOCK_MS,
                "time_budget_ms": DEFAULT_TIME_BUDGET_MS,
                "concurrency_per_partition": DEFAULT_CONCURRENCY,
            },
            "field_diff_count": len(diffs),
            "field_diffs": diffs[:50],
            "hash_mismatches": hash_mismatches,
            "fresh_summary": {
                "partition_watermarks": len(fresh_status["partition_watermarks"]),
                "subscription_cursors": len(fresh_status["subscription_cursors"]),
                "patches": len(fresh_status["patches"]),
                "diagnostic_runs": len(fresh_status["diagnostic_runs"]),
                "diagnostic_counts": expected["diagnostic_run"]["counts"],
            },
            "current_database": current,
            "patch_replay_same_ids": [patch["patch_id"] for patch in patch_result.get("patches", diagnose_result.get("patches", []))][:20],
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _verify_current_database(db_path: Path, bundle: Dict[str, Any]) -> Dict[str, Any]:
    if not db_path.exists():
        return {"checked": False, "ok": True, "reason": "database does not exist yet"}
    storage = Storage(db_path)
    try:
        status = storage.status()
    except Exception as exc:  # verification should report rather than crash
        return {"checked": True, "ok": False, "mismatches": [{"kind": "database_open", "message": str(exc)}]}
    mismatches: List[Dict[str, Any]] = []
    for record in status["partition_watermarks"]:
        view = storage.view_by_hash(record["semantic_view_hash"])
        if view is None:
            mismatches.append({"kind": "missing_historical_view", "partition_key": record["partition_key"], "view_hash": record["semantic_view_hash"]})
            continue
        recomputed = compute_all_watermarks(bundle, view, record["as_of_clock_ms"])
        actual = recomputed.get(record["partition_key"])
        if actual != record:
            mismatches.append(
                {
                    "kind": "watermark_field_mismatch",
                    "partition_key": record["partition_key"],
                    "diff": stable_diff(record, actual, "$.watermark")[:20],
                }
            )
    mismatches.extend(_hash_mismatches(status["patches"], "patch"))
    mismatches.extend(_hash_mismatches(status["invalidation_event_chains"], "closure"))
    mismatches.extend(_hash_mismatches(status["diagnostic_runs"], "diagnostic_run"))
    patch_ids = {patch["patch_id"] for patch in status["patches"]}
    for cursor in status["subscription_cursors"]:
        for patch_id in cursor.get("patch_ids", []):
            if patch_id not in patch_ids:
                mismatches.append({"kind": "cursor_patch_missing", "subscriber_id": cursor["subscriber_id"], "patch_id": patch_id})
    return {
        "checked": True,
        "ok": not mismatches,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:50],
        "partition_watermarks": len(status["partition_watermarks"]),
        "cursors": len(status["subscription_cursors"]),
        "patches": len(status["patches"]),
    }
