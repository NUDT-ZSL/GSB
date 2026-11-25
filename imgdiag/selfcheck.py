from __future__ import annotations

import os
from typing import Any

import numpy as np

from .hashing import canonical_hash
from .numeric import recompute_stage_diagnostics
from .pipeline import run_edit
from .workbench import STAGE_NAMES, CheckpointedWorkbench


def _strip_profile_prefix(arrays: dict[str, np.ndarray], profile: str) -> dict[str, np.ndarray]:
    prefix = f"{profile}__"
    return {key[len(prefix):]: value for key, value in arrays.items() if key.startswith(prefix)}


def query_edit(
    edit_id: str,
    tasks_by_id: dict[str, dict[str, Any]],
    params: dict[str, Any],
    workdir: str,
) -> dict[str, Any]:
    task = tasks_by_id[edit_id]
    wb = CheckpointedWorkbench(workdir)
    ledger_entry = wb.ledger["tasks"].get(edit_id)
    block_override = None
    if ledger_entry and ledger_entry.get("runtime_block_size"):
        block_override = int(ledger_entry["runtime_block_size"])
    fresh = run_edit(task, params, block_size_override=block_override,
                     profiles=("float64", "float32", "fixed"))
    if "summary" not in fresh:
        return {"edit_id": edit_id, "replay_match": False, "fresh": fresh}
    fresh_summary = fresh["summary"]
    result: dict[str, Any] = {
        "edit_id": edit_id,
        "fresh_summary": fresh_summary,
        "ledger_present": ledger_entry is not None,
    }
    if not ledger_entry:
        result["replay_match"] = False
        return result

    stage_artifacts = {}
    for stage_name, artifact_hash in ledger_entry.get("stage_hashes", {}).items():
        npz_path = wb.cache.object_npz_path(artifact_hash)
        stage_artifacts[stage_name] = {
            "artifact_hash": artifact_hash,
            "json_path": wb.cache.object_json_path(artifact_hash),
            "npz_path": npz_path if os.path.exists(npz_path) else None,
        }
    result["stage_artifacts"] = stage_artifacts

    ledger_summary = ledger_entry.get("summary", {})
    hash_match = ledger_summary.get("hashes", {}).get("float64_output") == fresh_summary["hashes"]["float64_output"]
    code_match = ledger_summary.get("code") == fresh_summary["code"] == "OK"

    detail_replay = {}
    stage_hashes = ledger_entry.get("stage_hashes", {})
    s5_hash = stage_hashes.get(STAGE_NAMES[-1])
    if s5_hash:
        arrays = wb.cache.load_arrays(s5_hash)
        for profile in ("float64", "float32", "fixed"):
            recomputed = recompute_stage_diagnostics(_strip_profile_prefix(arrays, profile))
            ledger_diag = ledger_summary.get("precision", {}).get(profile, {})
            same_dominant = recomputed["dominant_error_level"] == ledger_diag.get("dominant_error_level")
            same_rmse = True
            for level, stats in recomputed["levels"].items():
                ledger_rmse = ledger_diag.get("levels", {}).get(level, {}).get("rmse")
                if ledger_rmse is None or abs(float(ledger_rmse) - float(stats["rmse"])) > 1e-12:
                    same_rmse = False
            detail_replay[profile] = {
                "dominant_match": same_dominant,
                "rmse_match": same_rmse,
                "recomputed_dominant": recomputed["dominant_error_level"],
            }
    diagnostic_match = all(item["dominant_match"] and item["rmse_match"] for item in detail_replay.values()) if detail_replay else False
    result.update({
        "ledger_code": ledger_entry.get("code"),
        "hash_match": hash_match,
        "code_match": code_match,
        "diagnostic_replay": detail_replay,
        "diagnostic_match": diagnostic_match,
        "replay_match": bool(hash_match and code_match and diagnostic_match),
    })
    return result


def self_check_workbench(
    workdir: str,
    tasks: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    replay_all_edits: bool = False,
) -> dict[str, Any]:
    wb = CheckpointedWorkbench(workdir)
    checks: list[dict[str, Any]] = []

    chain = wb.checkpoints.verify_chain()
    checks.append({"name": "checkpoint_chain", "ok": bool(chain["chain_valid"]), **chain})

    block_failures = []
    stage_set_failures = []
    for edit_id, entry in wb.ledger["tasks"].items():
        if entry.get("status") == "committed":
            summary = entry.get("summary", {})
            if not summary.get("block_match", {}).get("byte_identical"):
                block_failures.append(edit_id)
            recomputed = set(entry.get("recomputed_stages", []))
            reused = set(entry.get("reused_stages", []))
            if not recomputed.isdisjoint(reused):
                stage_set_failures.append({"edit_id": edit_id, "reason": "overlap"})
            if not recomputed | reused == set(STAGE_NAMES):
                stage_set_failures.append({"edit_id": edit_id, "reason": "stage union incomplete",
                                           "missing": sorted(set(STAGE_NAMES) - recomputed - reused)})
    checks.append({
        "name": "block_reference_consistency",
        "ok": not block_failures,
        "failure_edit_ids": block_failures,
        "committed_count": sum(1 for e in wb.ledger["tasks"].values() if e.get("status") == "committed"),
    })
    checks.append({
        "name": "recompute_reuse_stage_sets",
        "ok": not stage_set_failures,
        "failures": stage_set_failures,
    })

    budget_failures = []
    budget_plans = wb.ledger.get("budget_plans", [])
    scheduling = {item["batch_index"]: item for item in wb.ledger.get("scheduling_records", [])}
    for plan in budget_plans:
        index = plan["batch_index"]
        record = scheduling.get(index)
        if record is None:
            budget_failures.append({"batch_index": index, "reason": "missing scheduling record"})
            continue
        admitted = [d["edit_id"] for d in plan["decisions"] if d["status"] in {"admitted", "degraded"}]
        if record["dispatch_order"] != sorted(admitted):
            budget_failures.append({"batch_index": index, "reason": "dispatch/admission mismatch"})
        if record["merge_order"] != sorted(admitted):
            budget_failures.append({"batch_index": index, "reason": "merge order mismatch"})
        for decision in plan["decisions"]:
            edit_id = decision["edit_id"]
            ledger_status = wb.ledger["tasks"].get(edit_id, {}).get("status")
            if decision["status"] in {"rejected", "invalid"} and ledger_status not in {"rejected", "invalid"}:
                budget_failures.append({"edit_id": edit_id, "reason": "rejected task has no rejection ledger status"})
            if decision["status"] in {"admitted", "degraded"}:
                artifact = wb.ledger["tasks"].get(edit_id, {})
                if artifact.get("status") != "committed":
                    budget_failures.append({"edit_id": edit_id, "reason": "admitted task not committed"})
    checks.append({"name": "budget_scheduling_ledger", "ok": not budget_failures, "failures": budget_failures})

    revision_plan = wb.ledger.get("revision_plan")
    if revision_plan is not None:
        revision_ok = bool(revision_plan.get("sets_disjoint") and revision_plan.get("union_covers_all_tasks"))
        checks.append({"name": "revision_sets", "ok": revision_ok, "plan": {
            "recompute": revision_plan.get("recompute_set"),
            "reuse": revision_plan.get("reuse_set"),
        }})

    tasks_by_id = {str(task["edit_id"]): task for task in tasks}
    replay_ids = sorted(
        edit_id for edit_id, entry in wb.ledger["tasks"].items()
        if entry.get("status") == "committed" and (replay_all_edits or edit_id == next(iter(tasks_by_id), None))
    )
    replay_failures = []
    for edit_id in replay_ids:
        if edit_id not in tasks_by_id:
            continue
        query = query_edit(edit_id, tasks_by_id, params, workdir)
        if not query.get("replay_match"):
            replay_failures.append(edit_id)
    checks.append({
        "name": "edit_query_independent_replay",
        "ok": not replay_failures,
        "checked_edit_ids": replay_ids,
        "failure_edit_ids": replay_failures,
    })

    ok = all(check["ok"] for check in checks)
    return {
        "ok": ok,
        "snapshot_version": wb.snapshot_version,
        "workdir": os.path.abspath(workdir),
        "checks": checks,
        "checkpoint": chain,
    }
