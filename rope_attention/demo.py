"""End-to-end deterministic demos used by the CLI and helper scripts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .attention import NumericTolerance
from .cache import CacheStore
from .component import RopeAttentionComponent
from .config import parse_config
from .data import (
    AttentionTask,
    load_tasks,
    load_tensors,
    lossy_v3_config,
    prepare_sample_workspace,
    sample_configs,
    verify_tensor_manifest,
)
from .engine import AttentionEngine, assert_results_byte_identical, retarget_tasks
from .errors import RopeError
from .migration import MigrationResult, migrate
from .models import FrequencyAdjustment, ParsedConfig, ResourceLimits
from .snapshot import ConfigSnapshot
from .util import canonical_json_bytes, hash_array, sha256_json


def _component(
    root: Path,
    *,
    limits: ResourceLimits | None = None,
    tolerance: NumericTolerance | None = None,
) -> RopeAttentionComponent:
    return RopeAttentionComponent(
        root,
        limits=limits,
        tolerance=tolerance,
        use_cache=True,
        validate_reference=True,
    )


def _load_inputs(root: Path) -> tuple[list[AttentionTask], dict[str, Any]]:
    tasks = load_tasks(root / "samples" / "tasks.jsonl")
    tensors = {
        task.case_id: load_tensors(
            root / "samples" / "tensors" / f"{task.case_id}.npz", task
        )
        for task in tasks
    }
    return tasks, tensors


def _migration_view(result: MigrationResult) -> dict[str, Any]:
    return result.to_dict()


def _snapshot_view(snapshot: ConfigSnapshot) -> dict[str, Any]:
    return {
        "checksum": snapshot.checksum,
        "config_version": snapshot.config.source_version,
        "source_name": snapshot.source_name,
        "version": snapshot.version,
    }


def _case_summary(result: Any) -> dict[str, Any]:
    return {
        "angles_hash": result.angles_hash,
        "block_deviation": (
            result.attention_comparison.to_dict()
            if result.attention_comparison is not None
            else None
        ),
        "business_codes": list(result.business_codes),
        "k_rot_hash": result.k_rot_hash,
        "output_hash": result.output_hash,
        "q_rot_hash": result.q_rot_hash,
        "rotation": result.rotation_summary,
    }


def _engine_summary(result: Any) -> dict[str, Any]:
    return {
        "affected_cases": list(result.affected_cases),
        "business_codes": list(result.business_codes),
        "cache_diagnostics": result.cache_diagnostics,
        "cases": {
            case_id: _case_summary(result.cases[case_id])
            for case_id in sorted(result.cases)
        },
        "degradations": list(result.degradations),
        "parallel_nodes_effective": result.parallel_nodes_effective,
        "parallel_nodes_requested": result.parallel_nodes_requested,
        "recomputed_nodes": list(result.recomputed_nodes),
        "reused_nodes": list(result.reused_nodes),
        "snapshot": {
            "checksum": result.snapshot_checksum,
            "config_version": result.config_version,
            "version": result.snapshot_version,
        },
    }


def _v3_with(
    config: ParsedConfig,
    *,
    block_size: int | None = None,
    pos_scale: float | None = None,
) -> str:
    document = {
        "base_theta": config.base_theta,
        "block_size": config.block_size if block_size is None else block_size,
        "frequency_adjustment": config.effective_frequency_adjustment.to_dict(),
        "per_head_scale": list(config.effective_per_head_scale),
        "pos_scale": config.pos_scale if pos_scale is None else pos_scale,
        "variant": config.effective_variant,
    }
    return "v3\n" + json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def _cache_key_audit(result: Any) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for case_id in sorted(result.cases):
        for event in result.cases[case_id].node_events:
            kind = event.node.split("[", 1)[0]
            if kind in seen_kinds:
                continue
            seen_kinds.add(kind)
            selected.append(event.to_dict())
    return selected


def _stage_record(
    *,
    label: str,
    component: RopeAttentionComponent,
    tasks: list[AttentionTask],
    tensors: dict[str, Any],
    snapshot: ConfigSnapshot,
    plan: dict[str, Any] | None,
    full_reference: Any,
    byte_check: dict[str, Any],
) -> dict[str, Any]:
    incremental = component.compute(tasks, tensors)
    return {
        "byte_equal_to_full_recompute": byte_check,
        "incremental": _engine_summary(incremental),
        "label": label,
        "plan": plan,
        "snapshot": _snapshot_view(snapshot),
        "full_reference_cache_diagnostics": full_reference.cache_diagnostics,
    }


def _run_incremental_demo(
    root: Path,
    tasks: list[AttentionTask],
    tensors: dict[str, Any],
    parsed: dict[str, ParsedConfig],
    migrated_v1_v3: MigrationResult,
) -> dict[str, Any]:
    runtime = root / "runtime" / "demo" / "incremental"
    component = _component(runtime)
    stages: list[dict[str, Any]] = []
    snapshot_history: list[dict[str, Any]] = []

    v1_snapshot, _ = component.commit_config(
        sample_configs()[ "v1" ],
        source_name="samples/configs/v1.conf",
        version=1,
    )
    snapshot_history.append(_snapshot_view(v1_snapshot))
    tasks_v1 = retarget_tasks(tasks, v1_snapshot.config)
    baseline = component.compute(tasks_v1, tensors)
    baseline_again = component.compute(tasks_v1, tensors)
    baseline_repro = {
        case_id: {
            "byte_identical": baseline.cases[case_id].byte_view()
            == baseline_again.cases[case_id].byte_view(),
            "output_hash": baseline.cases[case_id].output_hash,
        }
        for case_id in sorted(baseline.cases)
    }

    full_engine = AttentionEngine(
        None,
        use_cache=False,
        validate_reference=True,
    )
    full_baseline = full_engine.compute_all(tasks_v1, tensors, v1_snapshot)
    byte_baseline = assert_results_byte_identical(baseline, full_baseline)
    stages.append(
        {
            "byte_equal_to_full_recompute": byte_baseline,
            "baseline_reproducibility": baseline_repro,
            "incremental": _engine_summary(baseline),
            "label": "baseline-v1",
            "plan": None,
            "snapshot": _snapshot_view(v1_snapshot),
        }
    )

    assert migrated_v1_v3.target_text is not None
    v3_snapshot, _ = component.commit_config(
        migrated_v1_v3.target_text,
        source_name="migration:v1->v2->v3",
        version=2,
    )
    snapshot_history.append(_snapshot_view(v3_snapshot))
    tasks_v3 = retarget_tasks(tasks, v3_snapshot.config)
    upgrade_plan = component.invalidation_plan(tasks_v3, v1_snapshot, v3_snapshot)
    upgrade_incremental = component.compute(tasks_v3, tensors)
    full_upgrade = full_engine.compute_all(tasks_v3, tensors, v3_snapshot)
    upgrade_bytes = assert_results_byte_identical(upgrade_incremental, full_upgrade)
    cross_version_bytes = {
        case_id: {
            "byte_identical_to_v1_baseline": baseline.cases[case_id].byte_view()
            == upgrade_incremental.cases[case_id].byte_view(),
            "v1_hash": baseline.cases[case_id].output_hash,
            "v3_hash": upgrade_incremental.cases[case_id].output_hash,
        }
        for case_id in sorted(task.case_id for task in tasks_v3)
    }
    stages.append(
        {
            "byte_equal_to_full_recompute": upgrade_bytes,
            "cross_version_output": cross_version_bytes,
            "incremental": _engine_summary(upgrade_incremental),
            "label": "upgrade-v1-to-v3-same-semantics",
            "plan": upgrade_plan,
            "snapshot": _snapshot_view(v3_snapshot),
        }
    )

    block9_text = _v3_with(v3_snapshot.config, block_size=9)
    v3_block9, _ = component.commit_config(
        block9_text,
        source_name="demo:block-size-change",
        version=3,
    )
    snapshot_history.append(_snapshot_view(v3_block9))
    tasks_block9 = retarget_tasks(tasks, v3_block9.config)
    block_plan = component.invalidation_plan(tasks_block9, v3_snapshot, v3_block9)
    block_incremental = component.compute(tasks_block9, tensors)
    full_block = full_engine.compute_all(tasks_block9, tensors, v3_block9)
    block_bytes = assert_results_byte_identical(block_incremental, full_block)
    stages.append(
        {
            "byte_equal_to_full_recompute": block_bytes,
            "incremental": _engine_summary(block_incremental),
            "label": "block-size-6-to-9",
            "plan": block_plan,
            "snapshot": _snapshot_view(v3_block9),
        }
    )

    scale_text = _v3_with(v3_block9.config, pos_scale=1.1)
    v3_scale, _ = component.commit_config(
        scale_text,
        source_name="demo:pos-scale-change",
        version=4,
    )
    snapshot_history.append(_snapshot_view(v3_scale))
    tasks_scale = retarget_tasks(tasks, v3_scale.config)
    scale_plan = component.invalidation_plan(tasks_scale, v3_block9, v3_scale)
    scale_incremental = component.compute(tasks_scale, tensors)
    full_scale = full_engine.compute_all(tasks_scale, tensors, v3_scale)
    scale_bytes = assert_results_byte_identical(scale_incremental, full_scale)
    stages.append(
        {
            "byte_equal_to_full_recompute": scale_bytes,
            "incremental": _engine_summary(scale_incremental),
            "label": "pos-scale-1.0-to-1.1",
            "plan": scale_plan,
            "snapshot": _snapshot_view(v3_scale),
        }
    )

    return {
        "active_snapshot": _snapshot_view(component.active_snapshot()),
        "cache_key_audit": _cache_key_audit(scale_incremental),
        "cache_diagnostics": component.cache_diagnostics(),
        "cache_verify": component.verify_cache(),
        "snapshot_history": snapshot_history,
        "stages": stages,
    }


def run_demo(root: Path) -> dict[str, Any]:
    root = root.resolve()
    shutil.rmtree(root / "runtime" / "demo", ignore_errors=True)
    prepare_sample_workspace(root)
    tasks, tensors = _load_inputs(root)
    configs = sample_configs()
    parsed = {version: parse_config(text) for version, text in configs.items()}

    migration_v1_v3 = migrate(parsed["v1"], "v3")
    migration_v1_v2 = migrate(parsed["v1"], "v2")
    migration_v2_v3 = migrate(parsed["v2"], "v3")
    migration_v3_v2 = migrate(parsed["v3"], "v2")
    migration_v2_v1 = migrate(parsed["v2"], "v1")
    lossy = migrate(parse_config(lossy_v3_config()), "v2")
    v2_ntk = parse_config(
        "v2\nbase_theta=10000.0|pos_scale=1.0|block_size=6|variant=ntk\n"
    )
    ntk_to_v1 = migrate(v2_ntk, "v1")

    version_runs: dict[str, Any] = {}
    for version in ("v1", "v2", "v3"):
        runtime = root / "runtime" / "demo" / f"version-{version}"
        component = _component(runtime)
        snapshot, _ = component.commit_config(
            configs[version],
            source_name=f"samples/configs/{version}.conf",
            version=1,
        )
        subset = [task for task in tasks if task.config_version == version]
        result = component.compute(subset, tensors)
        result_again = component.compute(subset, tensors)
        reproducibility = {
            case_id: {
                "byte_identical": result.cases[case_id].byte_view()
                == result_again.cases[case_id].byte_view(),
                "output_hash": result.cases[case_id].output_hash,
            }
            for case_id in sorted(result.cases)
        }
        version_runs[version] = {
            **_engine_summary(result),
            "config_view": parsed[version].to_dict(),
            "reproducibility": reproducibility,
        }

    incremental = _run_incremental_demo(
        root,
        tasks,
        tensors,
        parsed,
        migration_v1_v3,
    )

    all_business_codes = {
        code
        for run in version_runs.values()
        for code in run["business_codes"]
    }
    all_business_codes.update(
        {
            migration_v1_v2.business_code,
            migration_v1_v3.business_code,
            migration_v2_v3.business_code,
            migration_v3_v2.business_code,
            migration_v2_v1.business_code,
            lossy.business_code,
            ntk_to_v1.business_code,
        }
    )
    incremental_codes = {
        code
        for stage in incremental["stages"]
        for code in stage["incremental"]["business_codes"]
    }
    all_business_codes.update(incremental_codes)

    return {
        "business_codes": sorted(all_business_codes),
        "cache_key_audit": incremental["cache_key_audit"],
        "cache_snapshot_control": {
            "active_snapshot": incremental["active_snapshot"],
            "cache_diagnostics": incremental["cache_diagnostics"],
            "cache_verify": incremental["cache_verify"],
            "snapshot_history": incremental["snapshot_history"],
        },
        "config_version_views": {
            version: {
                "canonical_sha256": sha256_json(parsed[version].to_dict()),
                "parsed": parsed[version].to_dict(),
                "raw_text": configs[version],
                "semantic_sha256": sha256_json(parsed[version].semantic_dict()),
                "source_version": parsed[version].source_version,
            }
            for version in ("v1", "v2", "v3")
        },
        "incremental": incremental,
        "migration": {
            "lossy_v3_to_v2": _migration_view(lossy),
            "v1_to_v2": _migration_view(migration_v1_v2),
            "v1_to_v3": _migration_view(migration_v1_v3),
            "v2_ntk_to_v1": _migration_view(ntk_to_v1),
            "v2_to_v1": _migration_view(migration_v2_v1),
            "v2_to_v3": _migration_view(migration_v2_v3),
            "v3_to_v2": _migration_view(migration_v3_v2),
        },
        "ok": True,
        "sample_manifest_checksum": json.loads(
            (root / "samples" / "tensor_manifest.json").read_text(encoding="utf-8")
        )["checksum"],
        "sample_verification": verify_tensor_manifest(root),
        "schema": "rope-attention-demo/v1",
        "version_runs": version_runs,
    }


def _capture(code: str, function: Any, *, expected_code: str) -> dict[str, Any]:
    try:
        value = function()
        business_code = (
            str(value.get("business_code", "OK"))
            if isinstance(value, dict)
            else "OK"
        )
        return {
            "business_code": business_code,
            "details": value if isinstance(value, dict) else {"value": value},
            "injected": code,
            "passed": business_code == expected_code,
            "raised": False,
        }
    except RopeError as exc:
        return {
            "business_code": exc.code,
            "details": exc.details,
            "injected": code,
            "message": exc.message,
            "passed": exc.code == expected_code,
            "raised": True,
        }


def run_fault_demo(root: Path) -> dict[str, Any]:
    root = root.resolve()
    prepare_sample_workspace(root)
    runtime = root / "runtime" / "fault-demo"
    shutil.rmtree(runtime, ignore_errors=True)
    tasks, tensors = _load_inputs(root)
    case_task = next(task for task in tasks if task.case_id == "case_a_causal")
    case_tensor = {case_task.case_id: tensors[case_task.case_id]}
    v1_text = sample_configs()["v1"]
    scenarios: list[dict[str, Any]] = []

    scenarios.append(
        _capture(
            "unknown-version",
            lambda: parse_config("v9\n10000|1.0|6\n").to_dict(),
            expected_code="E_VERSION_UNSUPPORTED",
        )
    )
    scenarios.append(
        _capture(
            "v1-field-count",
            lambda: parse_config("v1\n10000|1.0\n").to_dict(),
            expected_code="E_FORMAT_CORRUPT",
        )
    )
    scenarios.append(
        _capture(
            "v1-unparseable-number",
            lambda: parse_config("v1\nnot-a-number|1.0|6\n").to_dict(),
            expected_code="E_FORMAT_CORRUPT",
        )
    )
    scenarios.append(
        _capture(
            "v2-missing-field",
            lambda: parse_config("v2\nbase_theta=10000|pos_scale=1.0\n").to_dict(),
            expected_code="E_FORMAT_CORRUPT",
        )
    )
    scenarios.append(
        _capture(
            "v3-lossy-downgrade",
            lambda: migrate(parse_config(lossy_v3_config()), "v2").to_dict(),
            expected_code="E_SEMANTIC_DRIFT",
        )
    )

    component = _component(runtime / "cache-corruption")
    snapshot, _ = component.commit_config(
        v1_text,
        source_name="fault:v1",
        version=1,
    )
    component.compute([case_task], case_tensor, snapshot=snapshot)
    positions = [
        event
        for event in component.compute([case_task], case_tensor, snapshot=snapshot).cases[
            case_task.case_id
        ].node_events
        if event.node == "position_angles"
    ][0]
    entry_path = component.cache.entry_path(positions.key)
    document = json.loads(entry_path.read_text(encoding="utf-8"))
    data_b64 = document["payload"]["arrays"]["angles"]["data_b64"]
    document["payload"]["arrays"]["angles"]["data_b64"] = (
        ("A" if data_b64[0] != "A" else "B") + data_b64[1:]
    )
    entry_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    scenarios.append(
        _capture(
            "cache-entry-corruption",
            lambda: component.compute(
                [case_task], case_tensor, snapshot=snapshot
            ).to_dict(),
            expected_code="E_CACHE_CORRUPT",
        )
    )

    conflict_component = _component(runtime / "snapshot-conflict")
    conflict_component.commit_config(
        v1_text,
        source_name="fault:first",
        version=1,
    )
    scenarios.append(
        _capture(
            "same-version-different-checksum",
            lambda: conflict_component.commit_config(
                sample_configs()["v2"],
                source_name="fault:conflict",
                version=1,
            )[0].to_dict(),
            expected_code="E_SNAPSHOT_CONFLICT",
        )
    )

    resource_component = _component(
        runtime / "resource-limit",
        limits=ResourceLimits(max_block_memory_bytes=1),
    )
    resource_snapshot, _ = resource_component.commit_config(
        v1_text,
        source_name="fault:resource",
        version=1,
    )
    scenarios.append(
        _capture(
            "block-memory-limit",
            lambda: resource_component.compute(
                [case_task], case_tensor, snapshot=resource_snapshot
            ).to_dict(),
            expected_code="E_RESOURCE_LIMIT",
        )
    )

    tolerance_component = _component(
        runtime / "tolerance",
        tolerance=NumericTolerance(atol=0.0, rtol=0.0),
    )
    tolerance_snapshot, _ = tolerance_component.commit_config(
        v1_text,
        source_name="fault:tolerance",
        version=1,
    )
    scenarios.append(
        _capture(
            "zero-tolerance",
            lambda: tolerance_component.compute(
                [case_task], case_tensor, snapshot=tolerance_snapshot
            ).to_dict(),
            expected_code="E_TOLERANCE_EXCEEDED",
        )
    )

    return {
        "business_codes": sorted(
            {
                scenario["business_code"]
                for scenario in scenarios
            }
        ),
        "ok": all(scenario["passed"] for scenario in scenarios),
        "scenarios": scenarios,
        "schema": "rope-attention-fault-demo/v1",
    }

