"""Repeatable offline acceptance demo for incremental repair."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from .config import normalize_config
from .engine import RepairEngine
from .errors import BusinessError, ErrorCode
from .mask import MaskEditor, MaskState, Region
from .samples import load_sample, write_ppm
from .snapshots import SnapshotStore


def run_demo(
    workdir: str | Path = "artifacts/runtime",
    data_dir: str | Path = "samples/data",
    *,
    fresh_cache: bool = True,
) -> dict[str, Any]:
    root = Path(workdir)
    cache_dir = root / "cache"
    images_dir = root / "images"
    reports_dir = root / "reports"
    images_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    if fresh_cache and cache_dir.exists():
        shutil.rmtree(cache_dir)

    base, mask_state, config = load_sample(data_dir)
    engine = RepairEngine(base=base, cache_dir=cache_dir, config=config)

    initial = engine.submit(mask_state)
    initial_compare = engine.compare_with_full(raise_on_mismatch=True)
    _save_step(reports_dir, images_dir, "00_initial", initial.image, initial.report, initial_compare)
    steps: list[dict[str, Any]] = [
        _step_summary("initial", initial.report, initial_compare, "00_initial.ppm")
    ]

    grown = MaskEditor.grow(mask_state, 1, 4)
    grow_result = engine.submit(
        grown,
        edit_record={
            "type": "demo_edit",
            "action": "grow",
            "region_id": 1,
            "radius": 4,
        },
    )
    grow_compare = engine.compare_with_full(raise_on_mismatch=True)
    _save_step(
        reports_dir,
        images_dir,
        "01_grow_region_1",
        grow_result.image,
        grow_result.report,
        grow_compare,
    )
    steps.append(
        _step_summary(
            "grow_region_1",
            grow_result.report,
            grow_compare,
            "01_grow_region_1.ppm",
        )
    )

    shrunk = MaskEditor.shrink(grown, 2, 3)
    shrink_result = engine.submit(
        shrunk,
        edit_record={
            "type": "demo_edit",
            "action": "shrink",
            "region_id": 2,
            "radius": 3,
        },
    )
    shrink_compare = engine.compare_with_full(raise_on_mismatch=True)
    _save_step(
        reports_dir,
        images_dir,
        "02_shrink_region_2",
        shrink_result.image,
        shrink_result.report,
        shrink_compare,
    )
    steps.append(
        _step_summary(
            "shrink_region_2",
            shrink_result.report,
            shrink_compare,
            "02_shrink_region_2.ppm",
        )
    )

    style_config = copy.deepcopy(config)
    style_config["style"]["style_strength"] = min(
        1.0, round(style_config["style"]["style_strength"] + 0.11, 4)
    )
    style_result = engine.submit(shrunk, style=style_config)
    style_compare = engine.compare_with_full(raise_on_mismatch=True)
    _save_step(
        reports_dir,
        images_dir,
        "03_style_strength",
        style_result.image,
        style_result.report,
        style_compare,
    )
    steps.append(
        _step_summary(
            "style_strength",
            style_result.report,
            style_compare,
            "03_style_strength.ppm",
        )
    )

    merged = MaskEditor.merge(shrunk, (3, 4), target_id=3)
    merge_result = engine.submit(
        merged,
        style=style_config,
        edit_record={
            "type": "demo_edit",
            "action": "merge",
            "region_ids": [3, 4],
            "target_id": 3,
        },
    )
    merge_compare = engine.compare_with_full(raise_on_mismatch=True)
    _save_step(
        reports_dir,
        images_dir,
        "04_merge_3_4",
        merge_result.image,
        merge_result.report,
        merge_compare,
    )
    steps.append(
        _step_summary(
            "merge_3_4",
            merge_result.report,
            merge_compare,
            "04_merge_3_4.ppm",
        )
    )

    snapshot_checks = _snapshot_guardrail_checks(mask_state, config)
    concurrency = _concurrency_guardrail(base, config, mask_state, root / "busy_cache")
    resource = _resource_guardrail(base, mask_state, root / "resource_cache")
    validation = _validation_guardrails(base, root / "validation_cache")
    inspect_entries = {
        str(region_id): engine.inspect_region(region_id) for region_id in merged.region_ids
    }
    snapshot_status = engine.snapshot_status()
    verify = engine.verify_snapshots()
    self_check = engine.self_check()

    acceptance = {
        "incremental_equals_full_for_all_steps": all(
            step["comparison"]["byte_identical"]
            and step["comparison"]["artifact_hashes_equal"]
            for step in steps
        ),
        "grow_recomputed_only_target": steps[1]["recomputed_regions"] == [1],
        "grow_reused_unrelated": set(steps[1]["reused_regions"]) == {2, 3, 4},
        "shrink_recomputed_only_target": steps[2]["recomputed_regions"] == [2],
        "shrink_reused_unrelated": set(steps[2]["reused_regions"]) == {1, 3, 4},
        "style_recomputed_only_style_and_blend": all(
            nodes == ["style_transfer", "blend_output"]
            for nodes in steps[3]["recomputed_nodes"].values()
        ),
        "merge_recomputed_only_target": steps[4]["recomputed_regions"] == [3],
        "snapshot_rollback_detected": snapshot_checks["rollback_code"]
        == ErrorCode.SNAPSHOT_ROLLBACK,
        "snapshot_corruption_detected": snapshot_checks["corrupt_code"]
        == ErrorCode.SNAPSHOT_CORRUPT,
        "stage_busy_detected": concurrency["code"] == ErrorCode.STAGE_BUSY,
        "resource_limit_detected": resource["code"] == ErrorCode.RESOURCE_LIMIT,
        "all_validation_codes_detected": all(
            item["observed_code"] == item["expected_code"]
            for item in validation.values()
        ),
        "snapshot_verify_ok": bool(verify["ok"]),
        "self_check_ok": bool(self_check["ok"]),
    }
    report = {
        "schema": "RF-DEMO-V1",
        "sample": {
            "base_hash": initial.report["base_hash"],
            "mask_checksum": mask_state.checksum,
            "style_checksum": initial.report["snapshots"]["style"]["checksum"],
            "shape": list(base.shape),
        },
        "steps": steps,
        "snapshot_checks": snapshot_checks,
        "concurrency_check": concurrency,
        "resource_limit_check": resource,
        "validation_checks": validation,
        "snapshot_status": snapshot_status,
        "snapshot_verify": verify,
        "self_check": self_check,
        "cache_inspection": inspect_entries,
        "acceptance": acceptance,
        "all_passed": all(acceptance.values()),
    }
    summary_path = root / "acceptance_report.json"
    summary_path.write_text(
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return report


def _step_summary(
    name: str,
    report: dict[str, Any],
    comparison: dict[str, Any],
    image_file: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "image_file": image_file,
        "output_hash": report["output_hash"],
        "edits": report["edits"],
        "invalidated_regions": report["invalidated_regions"],
        "recomputed_regions": report["recomputed_regions"],
        "reused_regions": report["reused_regions"],
        "removed_regions": report["removed_regions"],
        "recomputed_nodes": report["recomputed_nodes"],
        "reused_nodes": report["reused_nodes"],
        "node_status": report["node_status"],
        "cache_hit_count": report["cache_hit_count"],
        "recomputed_node_count": report["recomputed_node_count"],
        "degraded": report["degraded"],
        "snapshots": report["snapshots"],
        "self_check": report["self_check"],
        "comparison": {
            "byte_identical": comparison["byte_identical"],
            "artifact_hashes_equal": comparison["artifact_hashes_equal"],
            "incremental_hash": comparison["incremental_hash"],
            "full_hash": comparison["full_hash"],
            "first_difference": comparison["first_difference"],
            "blend_deviation_spectrum": comparison["blend_deviation_spectrum"],
        },
    }


def _save_step(
    reports_dir: Path,
    images_dir: Path,
    stem: str,
    image: Any,
    report: dict[str, Any],
    comparison: dict[str, Any],
) -> None:
    write_ppm(images_dir / f"{stem}.ppm", image)
    payload = {
        "report": report,
        "full_comparison": comparison,
    }
    (reports_dir / f"{stem}.json").write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _snapshot_guardrail_checks(
    mask_state: MaskState,
    config: dict[str, Any],
) -> dict[str, Any]:
    store = SnapshotStore()
    store.commit("mask", 1, mask_state)
    grown = MaskEditor.grow(mask_state, 1, 1)
    store.commit("mask", 2, grown)
    rollback_code = None
    try:
        store.commit("mask", 1, mask_state)
    except BusinessError as exc:
        rollback_code = exc.code
    corrupt_code = None
    try:
        store.commit("mask", 3, grown, expected_checksum="00" * 32)
    except BusinessError as exc:
        corrupt_code = exc.code
    return {
        "rollback_code": rollback_code,
        "corrupt_code": corrupt_code,
        "current": store.status()["mask"],
        "note": "checks run on an isolated snapshot store",
    }


def _concurrency_guardrail(
    base: Any,
    config: dict[str, Any],
    mask_state: MaskState,
    cache_dir: Path,
) -> dict[str, Any]:
    engine = RepairEngine(base=base, cache_dir=cache_dir, config=config)
    engine.stage_lock.acquire()
    code = None
    try:
        engine.submit(mask_state)
    except BusinessError as exc:
        code = exc.code
    finally:
        engine.stage_lock.release()
    return {"code": code}


def _resource_guardrail(
    base: Any,
    mask_state: MaskState,
    cache_dir: Path,
) -> dict[str, Any]:
    limited = normalize_config(
        {
            "limits": {
                "max_regions": 2,
            }
        }
    )
    engine = RepairEngine(base=base, cache_dir=cache_dir, config=limited)
    code = None
    details: dict[str, Any] = {}
    try:
        engine.submit(mask_state)
    except BusinessError as exc:
        code = exc.code
        details = exc.details
    return {"code": code, "details": details}


def _validation_guardrails(base: Any, cache_dir: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    shape = base.shape[:2]
    mask = MaskState.from_mapping({1: _rectangle(shape, 2, 2, 8, 8)})

    def capture(name: str, expected: str, fn: Any) -> None:
        observed = None
        details: dict[str, Any] = {}
        try:
            fn()
        except BusinessError as exc:
            observed = exc.code
            details = exc.details
        results[name] = {
            "expected_code": expected,
            "observed_code": observed,
            "details": details,
        }

    capture(
        "duplicate_region",
        ErrorCode.DUPLICATE_REGION,
        lambda: MaskState(
            (
                Region(1, mask.regions[0].mask),
                Region(1, mask.regions[0].mask),
            )
        ),
    )
    capture(
        "empty_mask",
        ErrorCode.EMPTY_MASK,
        lambda: RepairEngine(base, cache_dir=cache_dir, config=None).submit(
            MaskState(())
        ),
    )
    capture(
        "region_out_of_bounds",
        ErrorCode.REGION_OUT_OF_BOUNDS,
        lambda: RepairEngine(
            base, cache_dir=cache_dir, config=None
        ).submit(MaskState.from_mapping({2: _rectangle((4, 4), 1, 1, 3, 3)})),
    )
    capture(
        "style_strength_range",
        ErrorCode.STYLE_STRENGTH_RANGE,
        lambda: normalize_config({"style": {"style_strength": 1.5}}),
    )
    capture(
        "unknown_precision",
        ErrorCode.UNKNOWN_PRECISION,
        lambda: normalize_config({"style": {"precision": "quantum"}}),
    )
    capture(
        "invalid_iterations",
        ErrorCode.INVALID_ITERATIONS,
        lambda: normalize_config({"style": {"iterations": 0}}),
    )
    capture(
        "invalid_context_radius",
        ErrorCode.INVALID_CONTEXT_RADIUS,
        lambda: normalize_config({"context_radius": 0}),
    )
    return results


def _rectangle(
    shape: tuple[int, int],
    y0: int,
    x0: int,
    y1: int,
    x1: int,
) -> Any:
    import numpy as np

    mask = np.zeros(shape, dtype=np.bool_)
    mask[y0:y1, x0:x1] = True
    return mask
