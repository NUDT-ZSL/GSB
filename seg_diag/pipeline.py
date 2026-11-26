"""End-to-end local diagnostic workflow and report generation."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from .cache import RowCache
from .codes import CODES
from .config_io import read_v1_config, read_v2_config
from .config_writer import write_v2_config
from .dependencies import plan_revision
from .diagnostics import build_error_report
from .errors import DiagnosticError
from .hashing import array_digest, canonical_bytes, content_key
from .ledger import build_ledger, ledger_canonical_bytes
from .migration import migrate_v1_to_v2, migrate_v2_to_v1
from .recovery import ForcedTermination, RecoveryRunner
from .sample_generator import (
    DEFAULT_SEED,
    build_sample_bundle,
    generate_sample_directory,
    load_sample_arrays,
)
from .selfcheck import run_self_check
from .versioning import SnapshotCoordinator, VersionedSnapshot


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def write_report(path: str | Path, report: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return path


def _prune_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key not in {"old_row_keys", "new_row_keys"}}


def _snapshot_payload(v1: Any, v2: Any) -> dict[str, Any]:
    return {
        "format": "seg-diag-config-snapshot/v1",
        "v1_checksum": v1.checksum,
        "v2_checksum": v2.checksum,
        "v1": v1.to_dict(),
        "v2": v2.to_dict(),
    }


def run_diagnostic_pipeline(
    workdir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
    regenerate: bool = True,
) -> dict[str, Any]:
    workdir = Path(workdir)
    sample_dir = workdir / "samples" / f"local_seed_{seed}"
    report_dir = workdir / "reports"
    runtime_dir = workdir / "runtime"
    report_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "error_report.json").unlink(missing_ok=True)

    generate_sample_directory(sample_dir, seed=seed, force=regenerate)
    arrays = load_sample_arrays(sample_dir)
    v1_config = read_v1_config(sample_dir / "config_v1.json")
    v2_config = read_v2_config(sample_dir / "config_v2")

    upgraded_v2, upgrade_report = migrate_v1_to_v2(v1_config)
    downgraded_v1_flatten, flatten_report = migrate_v2_to_v1(v2_config, policy="flatten")
    downgraded_v1_discard, discard_report = migrate_v2_to_v1(v2_config, policy="discard")

    initial_ledger = build_ledger(
        arrays, v1_config, v2_config, seed=seed, ledger_version=1
    )
    (report_dir / "ledger.v1.json").write_bytes(ledger_canonical_bytes(initial_ledger))

    cache_dir = runtime_dir / "row_cache"
    shutil.rmtree(cache_dir, ignore_errors=True)
    cache = RowCache(cache_dir)
    for row in initial_ledger["rows"]:
        cache.put(row)

    coordinator = SnapshotCoordinator(runtime_dir / "stage.lock")
    snapshot_v1 = VersionedSnapshot.create(_snapshot_payload(v1_config, v2_config), version=1)
    with coordinator.begin_submission(snapshot_v1) as lease:
        lease.commit()

    rev2_mappings = tuple(
        replace(mapping, confidence_threshold=0.93)
        if mapping.instance_id == 201
        else mapping
        for mapping in v2_config.mappings
    )
    rev2_config = replace(
        v2_config,
        revision=2,
        mappings=rev2_mappings,
        effective_at_ms=v2_config.effective_at_ms + 1000,
    )
    rev2_config.checksum = content_key(rev2_config.canonical_payload())
    write_v2_config(sample_dir / "config_v2_rev2_threshold", rev2_config)

    held_lease = coordinator.begin_submission(
        VersionedSnapshot.create(_snapshot_payload(v1_config, rev2_config), version=2)
    )
    conflict: dict[str, Any] | None = None
    started = time.perf_counter()
    try:
        coordinator.begin_submission(
            VersionedSnapshot.create(_snapshot_payload(v1_config, rev2_config), version=2)
        )
    except DiagnosticError as exc:
        conflict = exc.to_dict()
    busy_elapsed = time.perf_counter() - started
    held_lease.discard()

    snapshot_v2 = VersionedSnapshot.create(_snapshot_payload(v1_config, rev2_config), version=2)
    replaced_snapshot = snapshot_v1.whole_replace(snapshot_v2)
    with coordinator.begin_submission(snapshot_v2) as lease:
        committed_snapshot = lease.commit()

    threshold_plan = plan_revision(arrays, v1_config, v2_config, rev2_config)
    cached_rows: dict[str, dict[str, Any]] = {}
    for row_id, key in threshold_plan["new_row_keys"].items():
        row = cache.get(key)
        if row is not None:
            cached_rows[row_id] = row
    threshold_ledger = build_ledger(
        arrays,
        v1_config,
        rev2_config,
        seed=seed,
        existing_rows=cached_rows,
        ledger_version=2,
    )
    for row_id in threshold_ledger["_computed_rows"]:
        cache.put(next(row for row in threshold_ledger["rows"] if row["row_id"] == row_id))
    threshold_cache_stats = {"hits": cache.hits, "misses": cache.misses}
    (report_dir / "ledger.v2-threshold.json").write_bytes(
        ledger_canonical_bytes(threshold_ledger)
    )

    rev3_mappings = tuple(
        replace(mapping, category_id=2) if mapping.instance_id == 301 else mapping
        for mapping in rev2_config.mappings
    )
    rev3_config = replace(rev2_config, revision=3, mappings=rev3_mappings)
    rev3_config.checksum = content_key(rev3_config.canonical_payload())
    write_v2_config(sample_dir / "config_v2_rev3_mapping", rev3_config)
    mapping_plan = plan_revision(arrays, v1_config, rev2_config, rev3_config)

    cached_rows_rev3: dict[str, dict[str, Any]] = {}
    for row_id, key in mapping_plan["new_row_keys"].items():
        row = cache.get(key)
        if row is not None:
            cached_rows_rev3[row_id] = row
    mapping_ledger = build_ledger(
        arrays,
        v1_config,
        rev3_config,
        seed=seed,
        existing_rows=cached_rows_rev3,
        ledger_version=3,
    )
    for row_id in mapping_ledger["_computed_rows"]:
        cache.put(next(row for row in mapping_ledger["rows"] if row["row_id"] == row_id))
    mapping_cache_stats = {
        "hits": cache.hits - threshold_cache_stats["hits"],
        "misses": cache.misses - threshold_cache_stats["misses"],
    }
    (report_dir / "ledger.v3-mapping.json").write_bytes(
        ledger_canonical_bytes(mapping_ledger)
    )

    snapshot_v3 = VersionedSnapshot.create(_snapshot_payload(v1_config, rev3_config), version=3)
    with coordinator.begin_submission(snapshot_v3) as lease:
        committed_snapshot = lease.commit()

    error_report = build_error_report(arrays, mapping_ledger, rev3_config)

    recovery_dir = runtime_dir / "recovery"
    shutil.rmtree(recovery_dir, ignore_errors=True)
    first_runner = RecoveryRunner(
        recovery_dir, arrays, v1_config, v2_config, seed=seed, region_size=4
    )
    forced_failure: dict[str, Any] | None = None
    try:
        first_runner.run(batch_size=2, fail_in_batch_index=1)
    except ForcedTermination as exc:
        failure_details = exc.args[1] if len(exc.args) > 1 else {}
        forced_failure = {"simulated": True, **failure_details}
    killed_checkpoint = first_runner.load_checkpoint()
    committed_before_kill = list(killed_checkpoint["completed_regions"]) if killed_checkpoint else []

    restarted_runner = RecoveryRunner(
        recovery_dir, arrays, v1_config, v2_config, seed=seed, region_size=4
    )
    recovery_result = restarted_runner.run(batch_size=2)
    final_recovery_checkpoint = recovery_result["checkpoint"]
    recovery_ledger = recovery_result["ledger"]

    third_runner = RecoveryRunner(
        recovery_dir, arrays, v1_config, v2_config, seed=seed, region_size=4
    )
    idle_recovery = third_runner.run(batch_size=2)

    recovery_disjoint = set(recovery_result["computed_regions"]).isdisjoint(committed_before_kill)
    if not recovery_disjoint:
        raise DiagnosticError(
            CODES.INCONSISTENT,
            "recovered run recomputed a committed region",
            {"committed": committed_before_kill},
        )

    self_check = run_self_check(
        arrays=arrays,
        v1_config=v1_config,
        v2_config=rev3_config,
        ledger=mapping_ledger,
        seed=seed,
        upgraded_v2_config=upgraded_v2,
        downgraded_v1_config=downgraded_v1_flatten,
        downgrade_policy="flatten",
        migration_v2_config=v2_config,
        dependency_plan=mapping_plan,
        incremental_ledger=mapping_ledger,
    )
    recovery_self_check = run_self_check(
        arrays=arrays,
        v1_config=v1_config,
        v2_config=v2_config,
        ledger=recovery_ledger,
        seed=seed,
        upgraded_v2_config=upgraded_v2,
        downgraded_v1_config=downgraded_v1_discard,
        downgrade_policy="discard",
        checkpoint=final_recovery_checkpoint,
        expected_region_count=16,
    )

    determinism_bytes_a = ledger_canonical_bytes(initial_ledger)
    rebuilt_initial = build_ledger(arrays, v1_config, v2_config, seed=seed, ledger_version=1)
    determinism_bytes_b = ledger_canonical_bytes(rebuilt_initial)
    byte_deterministic = determinism_bytes_a == determinism_bytes_b

    fresh_bundle = build_sample_bundle(seed)
    seed_array_checks = {
        key: array_digest(fresh_bundle[key]) == array_digest(arrays[key])
        for key in [
            "image",
            "templates",
            "gt_instance",
            "gt_category",
            "v1_instance",
            "v2_category",
            "v2_instance",
            "v2_confidence",
        ]
    }
    fresh_ledger = build_ledger(
        fresh_bundle,
        fresh_bundle["v1_config"],
        fresh_bundle["v2_config"],
        seed=seed,
        ledger_version=1,
    )
    independent_seed_recompute = {
        "business_code": CODES.OK,
        "all_array_checks_match": all(seed_array_checks.values()),
        "array_checks": seed_array_checks,
        "ledger_checksum_matches": fresh_ledger["checksum"] == initial_ledger["checksum"],
        "ledger_bytes_match": ledger_canonical_bytes(fresh_ledger) == determinism_bytes_a,
    }
    if not independent_seed_recompute["ledger_bytes_match"]:
        raise DiagnosticError(
            CODES.INCONSISTENT,
            "fixed-seed independent reconstruction does not reproduce the ledger",
            independent_seed_recompute,
        )

    report = {
        "business_code": CODES.OK,
        "seed": seed,
        "sample_dir": str(sample_dir),
        "image": {"height": arrays["image"].shape[0], "width": arrays["image"].shape[1], "channels": 3},
        "configs": {
            "v1_checksum": v1_config.checksum,
            "v2_checksum": v2_config.checksum,
            "v2_rev2_threshold_checksum": rev2_config.checksum,
            "v2_rev3_mapping_checksum": rev3_config.checksum,
        },
        "migration": {
            "v1_to_v2": upgrade_report.to_dict(),
            "v2_to_v1_flatten": flatten_report.to_dict(),
            "v2_to_v1_discard": discard_report.to_dict(),
        },
        "semantic_ledger": {
            "format": mapping_ledger["format"],
            "ledger_version": mapping_ledger["ledger_version"],
            "checksum": mapping_ledger["checksum"],
            "byte_checksum": mapping_ledger["byte_checksum"],
            "row_count": len(mapping_ledger["rows"]),
            "byte_deterministic": byte_deterministic,
            "winner_counts": _winner_counts(mapping_ledger),
            "path": str(report_dir / "ledger.v3-mapping.json"),
        },
        "snapshot_versioning": {
            "initial_version": snapshot_v1.version,
            "revised_version": committed_snapshot.version,
            "initial_checksum": snapshot_v1.checksum,
            "active_checksum": coordinator.active.checksum if coordinator.active else None,
            "whole_replace_checksum": replaced_snapshot.checksum,
            "nonblocking_conflict": conflict,
            "busy_wait_seconds": busy_elapsed,
        },
        "threshold_revision": {
            "revised_instance_id": 201,
            "new_threshold": 0.93,
            "plan": _prune_plan(threshold_plan),
            "actual_computed_rows": len(threshold_ledger["_computed_rows"]),
            "actual_reused_rows": len(threshold_ledger["_reused_rows"]),
            "cache_hits": threshold_cache_stats["hits"],
            "cache_misses": threshold_cache_stats["misses"],
        },
        "mapping_revision": {
            "revised_instance_id": 301,
            "new_category_id": 2,
            "plan": _prune_plan(mapping_plan),
            "actual_computed_rows": len(mapping_ledger["_computed_rows"]),
            "actual_reused_rows": len(mapping_ledger["_reused_rows"]),
            "cache_hits": mapping_cache_stats["hits"],
            "cache_misses": mapping_cache_stats["misses"],
        },
        "error_spectrum": error_report,
        "recovery": {
            "state_dir": str(recovery_dir),
            "forced_failure": forced_failure,
            "computed_during_failed_attempt": list(first_runner.last_computed_regions),
            "committed_before_kill": committed_before_kill,
            "restart_computed_regions": recovery_result["computed_regions"],
            "restart_reused_committed_regions": recovery_result["reused_committed_regions"],
            "restart_committed_batches": recovery_result["committed_batches"],
            "committed_region_recompute_disjoint": recovery_disjoint,
            "idle_restart_computed_regions": idle_recovery["computed_regions"],
            "final_checkpoint": final_recovery_checkpoint,
        },
        "self_check": self_check,
        "recovery_self_check": recovery_self_check,
        "independent_seed_recompute": independent_seed_recompute,
    }
    write_report(report_dir / "diagnostic_report.json", report)
    write_report(report_dir / "dependency_plan_threshold.json", threshold_plan)
    write_report(report_dir / "dependency_plan_mapping.json", mapping_plan)
    return report


def _winner_counts(ledger: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in ledger["rows"]:
        source = row["winner"]["source"]
        counts[source] = counts.get(source, 0) + 1
    return dict(sorted(counts.items()))
