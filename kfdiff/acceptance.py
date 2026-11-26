"""Deterministic local acceptance run producing a structured verification report."""
from __future__ import annotations

import json
import platform
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from .errors import KFError
from .seed import generate_seed_files
from .selfcheck import run_selfcheck


def _capture(fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        value = fn()
        return {"ok": True, "value": value}
    except KFError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message, "details": exc.details}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": type(exc).__name__, "message": str(exc)}


def _run_in_thread(fn: Callable[[], Any]) -> Dict[str, Any]:
    box: Dict[str, Any] = {}

    def target() -> None:
        outcome = _capture(fn)
        box.update(outcome)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=30)
    return box


def run_acceptance(root: Optional[Path | str] = None, *, reset_cache: bool = True) -> Dict[str, Any]:
    root = Path(root).resolve() if root else Path(tempfile.mkdtemp(prefix="kfacc_"))
    root.mkdir(parents=True, exist_ok=True)
    assertions: List[Dict[str, Any]] = []

    def check(name: str, passed: bool, detail: Any = None) -> bool:
        assertions.append({"name": name, "passed": bool(passed), "detail": detail})
        return bool(passed)

    seed_info = generate_seed_files(root)
    if reset_cache:
        shutil.rmtree(root / "cache", ignore_errors=True)

    from .engine import Studio

    studio = Studio(root)
    started = time.time()

    # ---- requirement 1: shape validation + determinism -------------------
    bad_shape = _capture(lambda: _parse_bad_shape())
    check("e_clip_shape_on_illegal_interval",
          (not bad_shape["ok"]) and bad_shape.get("code") == "E_CLIP_SHAPE",
          bad_shape.get("code"))

    generation_rows: Dict[str, Any] = {}
    for clip_id in sorted(studio.tasks):
        result = studio.generate(clip_id)
        generation_rows[clip_id] = {
            "selected_block_grid": result["plan"]["selected_block_grid"],
            "selected_strength": result["plan"]["selected_strength"],
            "status": result["plan"]["status"],
            "snapshot_version": result["snapshot_version"],
            "first_block_timing": result["first_block_timing"],
            "reasons": result["plan"]["reasons"],
        }

    det_a = _all_pixel_hashes(studio)
    studio_b = Studio(root)
    det_b = _all_pixel_hashes(studio_b)
    check("deterministic_byte_identical_keyframes", det_a == det_b,
          {"clip_count": len(det_a), "total_keyframes": sum(len(v) for v in det_a.values())})

    # ---- requirement 2: partial queries + timing records ------------------
    partial = _partial_query_demo(studio)
    check("partial_ready_query_before_completion",
          partial["mid_ready"] > 0 and partial["mid_ready"] < partial["total"]
          and partial["final_complete"], partial)

    timing_present = all(
        row["first_block_timing"] and row["first_block_timing"]["observed_first_block_ms"] >= 0
        and "estimated_first_block_ms" in row["first_block_timing"]
        for row in generation_rows.values()
    )
    check("first_block_observed_and_estimated", timing_present)

    # ---- requirement 3: block vs whole consistency + fault location ------
    consistency_rows: Dict[str, Any] = {}
    all_consistent = True
    for clip_id in sorted(studio.tasks):
        verdict = studio.adjudicate_consistency(clip_id, raise_on_mismatch=False)
        consistency_rows[clip_id] = {
            "exact_match": verdict["exact_match"],
            "compared_keyframes": verdict["pixel_verdict"].get("compared_keyframes"),
            "boundary_exact": verdict["boundary_verdict"]["exact_match"],
        }
        all_consistent &= bool(verdict["exact_match"])
    check("block_equals_whole_all_clips", all_consistent, consistency_rows)

    fault = _fault_injection_demo(studio)
    fault_ok = (not fault["ok"]) and fault.get("code") == "E_BLOCK_MISMATCH"
    mismatch_loc = (fault.get("details", {}) or {}).get("pixel_verdict", {}).get("first_mismatch")
    location_ok = bool(mismatch_loc and mismatch_loc.get("frame_index") is not None
                       and mismatch_loc.get("pixel_coordinate_hwc") == [3, 4, 0])
    check("e_block_mismatch_locates_first_pixel", fault_ok and location_ok,
          {"code": fault.get("code"), "location": mismatch_loc})

    # ---- requirement 4: reproducible trade-off adjudication --------------
    plan_hash_1 = studio.build_plan("clip_0004")["plan_hash"]
    plan_hash_2 = studio.build_plan("clip_0004")["plan_hash"]
    check("tradeoff_plan_reproducible", plan_hash_1 == plan_hash_2, {"plan_hash": plan_hash_1})

    priority_matrix = _priority_matrix(studio)
    matrix_reproducible = all(
        row["hash_a"] == row["hash_b"] for row in priority_matrix
    )
    check("priority_order_verdicts_reproduce", matrix_reproducible, priority_matrix)

    # ---- requirement 5: targeted invalidation propagation -----------------
    studio.apply_revision_event("evt_rev2_targeted_noise")
    targeted = studio.generate("clip_0003")
    prop = targeted["propagation"]
    targeted_ok = (
        [k["ordinal"] for k in prop["recomputed_keyframes"]] == [5]
        and prop["recomputed_blocks"] == [1]
        and prop["recomputed_segments"] == [4, 5]
        and set(prop["reused_blocks"]) == {0, 2, 3}
    )
    check("targeted_noise_invalidation_sets", targeted_ok, {
        "recomputed_keyframes": [k["ordinal"] for k in prop["recomputed_keyframes"]],
        "recomputed_blocks": prop["recomputed_blocks"],
        "reused_blocks": prop["reused_blocks"],
        "recomputed_segments": prop["recomputed_segments"],
    })

    cross_clip = studio.cross_clip_cache_audit("clip_0003")
    cross_ok = all(c["artifacts_intact"] and c["cache_state"] == "reused" for c in cross_clip)
    check("other_clips_caches_reused", cross_ok, cross_clip)

    studio.apply_revision_event("evt_rev2_prompt_embedding")
    prompt_rev = studio.generate("clip_0004")["propagation"]
    prompt_ok = (len(prompt_rev["recomputed_keyframes"]) == studio.tasks["clip_0004"].keyframe_count
                 and len(prompt_rev["reused_keyframes"]) == 0)
    check("prompt_revision_recomputes_clip_embedding", prompt_ok, {
        "recomputed": len(prompt_rev["recomputed_keyframes"]),
    })

    unaffected_rerun = studio.generate("clip_0001")["propagation"]
    reuse_kinds = {k["reuse"] for k in unaffected_rerun["reused_keyframes"]}
    check("unaffected_clip_exact_cache_hit",
          not unaffected_rerun["recomputed_keyframes"] and reuse_kinds == {"exact_hit"},
          {"reuse_kinds": sorted(reuse_kinds)})

    # ---- requirement 6: content cache + snapshot rollback -----------------
    versions = [v["version"] for v in studio.snapshots.list_versions("clip_0003")]
    rollback = _capture(lambda: studio.snapshots.request_rollback("clip_0003", 1))
    check("e_snapshot_rollback_on_backward_request",
          (not rollback["ok"]) and rollback.get("code") == "E_SNAPSHOT_ROLLBACK",
          {"versions": versions, "code": rollback.get("code")})

    stale = _capture(lambda: studio.snapshots.commit_new(
        "clip_0003", 2, {"stale": True}, expected_base_version=1))
    check("stale_snapshot_writer_rejected",
          (not stale["ok"]) and stale.get("code") == "E_SNAPSHOT_ROLLBACK", stale.get("code"))

    snapshot_verify = _capture(lambda: studio.snapshots.verify_current("clip_0003"))
    check("snapshot_checksums_verify", snapshot_verify["ok"], snapshot_verify.get("value"))

    # ---- requirement 7: concurrency + resource limits ---------------------
    concurrency = _concurrency_and_limits()
    for assertion in concurrency["assertions"]:
        check(assertion["name"], assertion["passed"], assertion.get("detail"))

    # ---- requirement 8: self-check ----------------------------------------
    selfcheck_report = run_selfcheck(studio, raise_on_error=False)
    check("cross_module_selfcheck_passes", selfcheck_report["ok"], {
        "checks_total": selfcheck_report["checks_total"],
        "checks_passed": selfcheck_report["checks_passed"],
        "checks_failed": selfcheck_report["checks_failed"],
    })

    elapsed = time.time() - started
    overall = all(item["passed"] for item in assertions)
    report = {
        "overall_pass": overall,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(elapsed, 3),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "workspace": str(root),
        "seed": seed_info,
        "assertion_count": len(assertions),
        "passed_count": sum(1 for a in assertions if a["passed"]),
        "assertions": assertions,
        "generation": generation_rows,
        "consistency": consistency_rows,
        "priority_matrix": priority_matrix,
        "invalidation": {
            "targeted": _jsonable(prop),
            "cross_clip": cross_clip,
            "prompt_revision_recomputed": len(prompt_rev["recomputed_keyframes"]),
        },
        "selfcheck": {k: selfcheck_report[k] for k in
                      ("ok", "clips_checked", "checks_total", "checks_passed", "checks_failed")},
        "concurrency_detail": {k: v for k, v in concurrency.items() if k != "assertions"},
        "cache_stats": studio.db.stats(),
        "resource_status": studio.governor.status(),
    }
    return report


def _parse_bad_shape() -> None:
    from .clipmodel import parse_task

    parse_task({
        "clip_id": "clip_bad", "total_frames": 10, "keyframe_interval": 4,
        "frame_shape": [8, 8, 3], "noise_seed": 1, "prompt": "x",
        "diffusion_steps": 4, "block_grid": 2, "revision": 1,
    })


def _all_pixel_hashes(studio: Any) -> Dict[str, List[str]]:
    hashes: Dict[str, List[str]] = {}
    for clip_id in sorted(studio.tasks):
        whole = studio.whole_reference(clip_id)
        hashes[clip_id] = [frame["provenance"]["pixels_hash"] for frame in whole["frames"]]
    return hashes


def _partial_query_demo(studio: Any) -> Dict[str, Any]:
    iterator = studio.generate_incremental("clip_0008")
    plan_event = next(iterator)
    block_event = next(iterator)  # first block ready
    mid = studio.query_ready("clip_0008")
    rest = list(iterator)
    final = studio.query_ready("clip_0008")
    return {
        "selected_grid": plan_event["plan"]["selected_block_grid"],
        "first_block_size": block_event["block"]["members"].__len__(),
        "mid_ready": mid["ready_count"],
        "total": mid["total_keyframes"],
        "mid_complete_flag": mid["complete"],
        "events_after_first": len(rest),
        "final_complete": final["complete"],
    }


def _fault_injection_demo(studio: Any) -> Dict[str, Any]:
    from .diffusion import generate_keyframe

    def faulty_generator(sources: Dict[str, Any], ordinal: int) -> Dict[str, Any]:
        produced = generate_keyframe(sources, ordinal)
        if ordinal == 2:
            produced["pixels"][3, 4, 0] = produced["pixels"][3, 4, 0] ^ np.uint8(0xFF)
        return produced

    return _capture(lambda: studio.adjudicate_consistency(
        "clip_0001", block_frame_generator=faulty_generator))


def _priority_matrix(studio: Any) -> List[Dict[str, Any]]:
    from itertools import permutations

    rows = []
    for order in permutations(["block_grid", "first_frame_budget", "diffusion_strength"]):
        plan_a = studio.build_plan("clip_0004", budget_ms=12.0, priority_order=list(order))
        plan_b = studio.build_plan("clip_0004", budget_ms=12.0, priority_order=list(order))
        rows.append({
            "priority_order": list(order),
            "selected_block_grid": plan_a["selected_block_grid"],
            "selected_strength": plan_a["selected_strength"],
            "status": plan_a["status"],
            "hash_a": plan_a["plan_hash"],
            "hash_b": plan_b["plan_hash"],
        })
    return rows


def _concurrency_and_limits() -> Dict[str, Any]:
    from .engine import Studio

    tmp = Path(tempfile.mkdtemp(prefix="kfconc_"))
    generate_seed_files(tmp)
    studio = Studio(tmp)
    studio.generate("clip_0001", parallel=False)
    assertions: List[Dict[str, Any]] = []

    # E_STAGE_BUSY: non-blocking conflicting submission
    with studio.governor.stage("acceptance-holder"):
        busy = _run_in_thread(lambda: studio.generate("clip_0002", parallel=False))
    assertions.append({
        "name": "e_stage_busy_conflicting_submission",
        "passed": (not busy["ok"]) and busy.get("code") == "E_STAGE_BUSY",
        "detail": busy.get("code"),
    })

    # concurrent queries overlap
    barrier = threading.Barrier(8)

    def query_worker() -> None:
        with studio.governor.query():
            barrier.wait(timeout=10)
            studio.query_ready("clip_0001")

    threads = [threading.Thread(target=query_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    max_seen = studio.governor.max_concurrent_queries_observed
    assertions.append({
        "name": "ready_queries_run_concurrently",
        "passed": max_seen >= 8,
        "detail": {"max_concurrent_queries_observed": max_seen},
    })

    # memory cap
    mem_studio = Studio(tmp, config_overrides={
        "memory": {"max_clip_resident_bytes": 1}
    })
    memory = _capture(lambda: mem_studio.generate("clip_0001", parallel=False))
    assertions.append({
        "name": "e_resource_limit_on_memory_cap",
        "passed": (not memory["ok"]) and memory.get("code") == "E_RESOURCE_LIMIT",
        "detail": memory.get("code"),
    })

    # parallel slots exhausted -> serial degradation
    ser_studio = Studio(tmp, config_overrides={
        "memory": {"max_parallel_jobs": 1, "on_parallel_limit": "serialize"}
    })
    occupied = ser_studio.governor.occupy_all_slots_for_test()
    try:
        serial = _capture(lambda: ser_studio.generate("clip_0002"))
        degraded = any(d["category"] == "parallel_to_serial"
                       for d in ser_studio.governor.degradations)
        assertions.append({
            "name": "parallel_limit_degrades_to_serial",
            "passed": serial["ok"] and degraded,
            "detail": {"occupied_slots": occupied, "degraded": degraded},
        })
    finally:
        for _ in range(occupied):
            ser_studio.governor.release_job_slot()

    # parallel slots exhausted -> reject policy
    rej_studio = Studio(tmp, config_overrides={
        "memory": {"max_parallel_jobs": 1, "on_parallel_limit": "reject"}
    })
    occupied = rej_studio.governor.occupy_all_slots_for_test()
    try:
        rejected = _capture(lambda: rej_studio.generate("clip_0003"))
        assertions.append({
            "name": "e_resource_limit_when_parallel_policy_reject",
            "passed": (not rejected["ok"]) and rejected.get("code") == "E_RESOURCE_LIMIT",
            "detail": rejected.get("code"),
        })
    finally:
        for _ in range(occupied):
            rej_studio.governor.release_job_slot()

    # checksum tamper detection
    tamper = _checksum_tamper_demo(tmp)
    assertions.append({
        "name": "snapshot_checksum_tamper_detected",
        "passed": (not tamper["ok"]) and tamper.get("code") == "E_CHECKSUM_MISMATCH",
        "detail": tamper.get("code"),
    })

    return {
        "assertions": assertions,
        "max_concurrent_queries_observed": max_seen,
        "temp_workspace": str(tmp),
    }


def _checksum_tamper_demo(tmp: Path) -> Dict[str, Any]:
    from .engine import Studio

    tamper_studio = Studio(tmp)
    tamper_studio.generate("clip_0004", parallel=False)
    manifest = tamper_studio.snapshots.current_manifest("clip_0004")
    digest = manifest["keyframes"][0]["content_hash"]
    path = tamper_studio.store._path_for(digest)
    path.write_bytes(b"tampered-content")
    return _capture(lambda: tamper_studio.snapshots.verify_current("clip_0004"))


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def write_report(report: Dict[str, Any], records_dir: Path | str) -> Path:
    records_dir = Path(records_dir)
    records_dir.mkdir(parents=True, exist_ok=True)
    target = records_dir / "acceptance_report.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str),
                      encoding="utf-8")
    return target
