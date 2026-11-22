from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .errors import ErrorCode, EngineError, fail
from .execution import compare_results, compute_truth, execute_shard
from .model import Experiment, canonical_json, load_experiment
from .planning import RunPlan, build_plan
from .scheduler import Scheduler
from .store import ReadonlyStore, Store


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare(experiment_path: str | Path) -> tuple[Experiment, RunPlan]:
    experiment = load_experiment(experiment_path)
    return experiment, build_plan(experiment)


def run(experiment_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    experiment, plan = prepare(experiment_path)
    store = Store(db_path)
    try:
        scheduler = Scheduler(store, experiment, plan)
        return scheduler.execute_new()
    finally:
        store.close()


def resume(experiment_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).resolve()
    if not path.exists():
        raise fail(
            ErrorCode.STATE_NOT_FOUND,
            "state database does not exist; run the experiment first",
            db_path=str(path),
        )
    experiment, plan = prepare(experiment_path)
    store = Store(path)
    try:
        scheduler = Scheduler(store, experiment, plan)
        return scheduler.execute_resume()
    finally:
        store.close()


def status(db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).resolve()
    if not path.exists():
        raise fail(
            ErrorCode.STATE_NOT_FOUND,
            "state database does not exist",
            db_path=str(path),
        )
    store = ReadonlyStore(path)
    try:
        return store.status()
    finally:
        store.close()


def verify(experiment_path: str | Path, db_path: str | Path) -> dict[str, Any]:
    path = Path(db_path).resolve()
    if not path.exists():
        raise fail(
            ErrorCode.STATE_NOT_FOUND,
            "state database does not exist",
            db_path=str(path),
        )
    experiment, plan = prepare(experiment_path)
    store = Store(path)
    try:
        store.verify_identity(experiment, plan)
        summary = store.run_summary()
        if summary["run"]["status"] != "COMPLETE":
            return {
                "ok": False,
                "code": ErrorCode.RUN_INCOMPLETE,
                "message": "run is not complete; there is no committed final result to verify",
                "run_status": summary["run"]["status"],
                "shard_counts": summary["shard_counts"],
            }
        state = store.status()
        shard_results = {shard["shard_id"]: shard for shard in state["shard_watermark"]["shards"]}
        if any(shard["status"] != "COMMITTED" for shard in shard_results.values()):
            return {
                "ok": False,
                "code": ErrorCode.RUN_INCOMPLETE,
                "message": "at least one shard is not committed",
                "shard_counts": state["shard_watermark"]["counts"],
            }
        for shard in plan.shard_plans:
            stored = shard_results.get(shard.shard_id)
            if stored is None:
                return {
                    "ok": False,
                    "code": ErrorCode.STATE_MISMATCH,
                    "message": "shard is missing from persisted state",
                    "shard_id": shard.shard_id,
                }
            if stored["shard_digest"] != shard.shard_digest:
                return {
                    "ok": False,
                    "code": ErrorCode.STATE_MISMATCH,
                    "message": "persisted shard descriptor digest differs from the deterministic plan",
                    "shard_id": shard.shard_id,
                    "expected": shard.shard_digest,
                    "actual": stored["shard_digest"],
                }
            if stored["content_digest"] != shard.content_digest:
                return {
                    "ok": False,
                    "code": ErrorCode.STATE_MISMATCH,
                    "message": "persisted shard content digest differs from the deterministic plan",
                    "shard_id": shard.shard_id,
                    "expected": shard.content_digest,
                    "actual": stored["content_digest"],
                }
            recomputed = execute_shard(experiment, plan, shard)
            recomputed_digest = _sha(canonical_json(recomputed))
            if stored["result_digest"] != recomputed_digest:
                return {
                    "ok": False,
                    "code": ErrorCode.VERIFY_FAILED,
                    "message": "persisted shard partial does not match deterministic re-execution",
                    "shard_id": shard.shard_id,
                    "expected": recomputed_digest,
                    "actual": stored["result_digest"],
                }
        persisted_groups, persisted_windows = store.read_results()
        truth_groups, truth_windows = compute_truth(experiment, plan)
        comparison = compare_results(
            persisted_groups,
            persisted_windows,
            truth_groups,
            truth_windows,
        )
        if not comparison["ok"]:
            return {
                "ok": False,
                "code": ErrorCode.VERIFY_FAILED,
                "message": "persisted result differs from single-stream truth",
                "first_diff": comparison["first_diff"],
                "shards_verified": len(plan.shard_plans),
            }
        sensitive_groups = sum(1 for item in persisted_groups if item.get("precision_sensitive"))
        sensitive_windows = sum(1 for item in persisted_windows if item.get("precision_sensitive"))
        fixed_overflow = sum(
            int(metric.get("error_spectrum", {}).get("fixed_overflow", 0))
            for item in persisted_groups + persisted_windows
            for metric in item.get("metrics", {}).values()
        )
        return {
            "ok": True,
            "run_status": "COMPLETE",
            "input_digest": experiment.digest,
            "plan_digest": plan.plan_digest,
            "mode": plan.mode,
            "shards_verified": len(plan.shard_plans),
            "groups_compared": comparison["groups_compared"],
            "windows_compared": comparison["windows_compared"],
            "precision_sensitive_groups": sensitive_groups,
            "precision_sensitive_windows": sensitive_windows,
            "fixed_overflow_observations": fixed_overflow,
            "logical_clock": summary["run"]["logical_clock"],
        }
    finally:
        store.close()
