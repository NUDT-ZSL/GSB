"""Repeatable end-to-end diagnostic demo (no external services).

The demo:

1. generates deterministic pages and runs the full graph (cold cache);
2. re-runs to prove whole-graph cache reuse and byte-identical outputs;
3. appends a page, corrects one truth line and changes the detection
   threshold, verifying only the affected suffixes recompute;
4. bumps the template library version, proving whole-snapshot replacement and
   selective invalidation (preprocess/detect/features stay reusable);
5. runs chunked recognition and asserts byte-identical per-line output;
6. exercises every business error path and the cross-module self checks.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict

from . import consistency
from .config import GRAPH_NODES, PipelineConfig, ResourceLimits
from .deterministic import content_hash
from .errors import ErrorCode, OcrBenchError
from .incremental import ChangeSet
from .workbench import OcrWorkbench


def _fresh_workbench(root: str, config: PipelineConfig) -> OcrWorkbench:
    if os.path.exists(root):
        shutil.rmtree(root)
    return OcrWorkbench(root, config)


def run_demo(root: str) -> Dict[str, Any]:
    report: Dict[str, Any] = {"steps": [], "errors_demonstrated": []}

    def log(step: str, **payload: Any) -> None:
        report["steps"].append({"step": step, **payload})

    config = PipelineConfig()
    wb = _fresh_workbench(root, config)

 # 1) cold run on deterministic pages
    seeds = [("page-001", 101), ("page-002", 202), ("page-003", 303)]
    wb.create_dataset(seeds, charset_version=1)
    cold = wb.run_all()
    cold_signatures = {
        page_id: {
            "recognize": content_hash(result.payloads["recognize"]),
            "aggregate": content_hash(result.payloads["aggregate"]),
        }
        for page_id, result in cold.items()
    }
    log(
        "cold_run",
        pages=sorted(cold),
        recomputed={
            page_id: result.recomputed_nodes() for page_id, result in cold.items()
        },
    )

    # 2) second run must fully reuse the cache and stay byte identical
    warm = wb.run_all()
    for page_id, result in warm.items():
        assert result.recomputed_nodes() == [], page_id
        assert content_hash(result.payloads["recognize"]) == cold_signatures[page_id]["recognize"]
        assert content_hash(result.payloads["aggregate"]) == cold_signatures[page_id]["aggregate"]
    log("warm_run", reused={page_id: result.reused_nodes() for page_id, result in warm.items()})

    # 3) incremental changes: append page, correct truth, change threshold
    wb.append_page("page-004", 404)
    original_first_truth = cold["page-001"].payloads["aggregate"]["per_line"][0]["truth"]
    corrected_first_truth = "Z" + original_first_truth[1:]
    wb.correct_truth("page-001", 0, corrected_first_truth)
    change_set = ChangeSet(
        new_page_ids={"page-004"},
        corrected_pages={"page-001": [0]},
    )
    plan = wb.plan_incremental(change_set)
    log(
        "incremental_plan",
        affected_pages=sorted(plan.affected_pages),
        recompute_nodes=plan.recompute_nodes,
        reused_nodes=plan.reused_nodes,
        affected_nodes_by_page=plan.affected_nodes_by_page,
    )
    incremental = wb.run_all()
    # Unaffected pages keep byte-identical recognize/aggregate payloads.
    for page_id in ("page-002", "page-003"):
        assert content_hash(incremental[page_id].payloads["recognize"]) == cold_signatures[page_id]["recognize"]
        assert content_hash(incremental[page_id].payloads["aggregate"]) == cold_signatures[page_id]["aggregate"]
    # The new page computes every node; the corrected page only recomputes aggregate.
    assert set(incremental["page-004"].recomputed_nodes()) == set(GRAPH_NODES)
    assert incremental["page-001"].recomputed_nodes() == ["aggregate"]
    # Reuse/recompute partition invariant for every page.
    for result in incremental.values():
        consistency.check_reuse_recompute_partition(result, set())
    log(
        "incremental_run",
        status={page_id: wb.node_status(result) for page_id, result in incremental.items()},
    )

    # Detection threshold change invalidates preprocess/detect suffix globally.
    threshold_config = PipelineConfig(detect_threshold=config.detect_threshold + 10)
    wb_threshold = OcrWorkbench(os.path.join(root, "threshold_wb"), threshold_config)
    wb_threshold.data_dir = wb.data_dir
    wb_threshold._pages = dict(wb._pages)
    threshold_plan = wb_threshold.plan_incremental(
        ChangeSet(detect_threshold_changed=True)
    )
    log(
        "threshold_plan",
        recompute_nodes=threshold_plan.recompute_nodes,
        reused_nodes=threshold_plan.reused_nodes,
    )

    # 4) template version bump: whole snapshot replacement + selective suffix
    bumped = PipelineConfig(charset_version=2)
    wb_v2 = OcrWorkbench(os.path.join(root, "v2_wb"), bumped)
    # Share the content cache so only genuinely key-changing nodes recompute.
    wb_v2.cache = wb.cache
    wb_v2.engine.cache = wb.cache
    wb_v2.data_dir = wb.data_dir
    wb_v2._pages = dict(wb._pages)
    before_version = wb_v2.template_version()
    run_v2 = wb_v2.run_all()
    after_version = wb_v2.template_version()
    # preprocess/detect/features reuse; match/recognize/aggregate recompute.
    for result in run_v2.values():
        assert "match" in result.recomputed_nodes()
        assert "recognize" in result.recomputed_nodes()
        assert "preprocess" in result.reused_nodes()
        assert "detect" in result.reused_nodes()
        assert "features" in result.reused_nodes()
    log(
        "template_bump",
        before_version=before_version,
        after_version=after_version,
        recomputed={page_id: result.recomputed_nodes() for page_id, result in run_v2.items()},
    )

    # Snapshot rollback must be refused.
    try:
        from .templates import build_library

        wb_v2.snapshots.replace(build_library(1))
    except OcrBenchError as error:
        assert error.code == ErrorCode.SNAPSHOT_ROLLBACK
        report["errors_demonstrated"].append(_error_entry(error))

    # Corrupted snapshot must be reported with its version.
    corrupt_version = wb_v2.snapshots.corrupt_for_diagnosis()
    try:
        wb_v2.snapshots.load()
    except OcrBenchError as error:
        assert error.code == ErrorCode.CACHE_CORRUPT
        assert error.details.get("version") == corrupt_version
        report["errors_demonstrated"].append(_error_entry(error))

    # 5) chunked vs whole-page byte identity across several chunk sizes
    chunk_results = []
    for chunk_size in (1, 2, 3):
        outcome = wb.run_chunked("page-001", chunk_size)
        assert outcome["identical"] is True
        chunk_results.append({"chunk_size": chunk_size, "identical": True})
    log("chunk_consistency", results=chunk_results)

    # 6) remaining error paths and self checks
    _demonstrate_key_invalid(wb, report)
    _demonstrate_stage_busy(wb, report)
    _demonstrate_resource_limit(root, report)
    _demonstrate_chunk_mismatch(root, report)
    _demonstrate_detect_empty(root, report)
    _demonstrate_attribution(root, report)

    # Cache key/hash cross-check plus aggregate independent recomputation.
    result = wb.run_page("page-001")
    for execution in result.executions:
        consistency.check_cache_key_and_hash(wb.cache, execution.key)
    consistency.check_aggregate_recomputation(result, wb._require_page("page-001").lines, wb.config)
    consistency.check_topology(wb.config)
    log("self_checks", passed=True)

    report["metrics"] = wb.metrics()
    report["template_version"] = wb.template_version()
    report["cache_entry_count"] = wb.cache.entry_count()

    report_path = os.path.join(root, "demo_report.json")
    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True, indent=2)
    return report


def _error_entry(error: OcrBenchError) -> Dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "page_id": error.page_id,
        "stage": error.stage,
        "details": error.details,
    }


def _demonstrate_key_invalid(wb: OcrWorkbench, report: Dict[str, Any]) -> None:
    try:
        wb.cache.build_key("match", {"node": "match", "page_id": "page-001"})
    except OcrBenchError as error:
        assert error.code == ErrorCode.CACHE_KEY_INVALID
        assert "missing_fields" in error.details
        report["errors_demonstrated"].append(_error_entry(error))


def _demonstrate_stage_busy(wb: OcrWorkbench, report: Dict[str, Any]) -> None:
    details = {
        "node": "recognize",
        "page_id": "page-lock",
        "pixel_hash": "a" * 64,
        "config_hash": "b" * 64,
        "upstream_hash": "c" * 64,
        "charset_version": 1,
        "dedup_threshold": 850,
        "line_box_summary": "d" * 16,
    }
    key = wb.cache.build_key("recognize", details)
    lock = wb.cache._acquire_stage(key)
    lock.acquire()
    try:
        try:
            wb.cache.commit("recognize", details, {"x": 1})
        except OcrBenchError as error:
            assert error.code == ErrorCode.STAGE_BUSY
            report["errors_demonstrated"].append(_error_entry(error))
    finally:
        lock.release()


def _demonstrate_resource_limit(root: str, report: Dict[str, Any]) -> None:
    tight = PipelineConfig(
        resource_limits=ResourceLimits(max_lines_per_page=2, max_cache_entries=5)
    )
    wb = _fresh_workbench(os.path.join(root, "resource_wb"), tight)
    wb.create_dataset([("page-r", 909)], charset_version=1)
    try:
        wb.run_page("page-r")
    except OcrBenchError as error:
        assert error.code == ErrorCode.RESOURCE_LIMIT
        report["errors_demonstrated"].append(_error_entry(error))


def _demonstrate_chunk_mismatch(root: str, report: Dict[str, Any]) -> None:
    from .engine import _first_text_mismatch

    whole = [{"text": "ABC"}, {"text": "DEFG"}]
    chunked = [{"text": "ABC"}, {"text": "DEXG"}]
    mismatch = _first_text_mismatch(whole, chunked)
    assert mismatch == (1, 2)
    report["errors_demonstrated"].append(
        {
            "code": ErrorCode.CHUNK_MISMATCH,
            "message": "synthetic first mismatch located",
            "page_id": None,
            "stage": "chunk",
            "details": {"first_bad_line": 1, "first_bad_char": 2},
        }
    )


def _demonstrate_detect_empty(root: str, report: Dict[str, Any]) -> None:
    import numpy as np

    from . import pipeline

    config = PipelineConfig()
    blank = np.full((40, 60), 255, dtype=np.uint8)
    pre = pipeline.preprocess(blank, config)
    try:
        pipeline.detect_lines(pre, "page-blank")
    except OcrBenchError as error:
        assert error.code == ErrorCode.DETECT_EMPTY
        assert error.page_id == "page-blank"
        report["errors_demonstrated"].append(_error_entry(error))


def _demonstrate_attribution(root: str, report: Dict[str, Any]) -> None:
    from .samplegen import load_manifest

    bad_dir = os.path.join(root, "bad_manifest")
    os.makedirs(bad_dir, exist_ok=True)
    with open(os.path.join(bad_dir, "manifest.jsonl"), "w", encoding="utf-8") as handle:
        handle.write('{"page_id": "broken"}\n')
    try:
        load_manifest(bad_dir)
    except OcrBenchError as error:
        assert error.code == ErrorCode.PARSE_FAILED
        report["errors_demonstrated"].append(_error_entry(error))
