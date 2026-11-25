from __future__ import annotations

import copy
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from .cache import CacheStore
from .checkpoint import SimulatedHardStop, run_checkpointed_batches
from .config import FeatureConfig, replace_config
from .engine import (
    Computation,
    add_cross_slot,
    apply_row_correction,
    change_categorical_codec,
    compare_score_sets,
    compute,
    remove_cross_slot,
)
from .errors import (
    E_BAD_INPUT,
    E_CHECKPOINT_CORRUPT,
    E_ENCODING_ERROR,
    E_INTERNAL_ERROR,
    E_LATENCY_BUDGET,
    E_PARAM_INVALID,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STALE_RESULT,
    EngineError,
)
from .generator import prepare_workspace
from .query import QueryService, query_many
from .sample import read_rows_csv, rows_digest
from .snapshot import (
    configs_semantically_equal,
    load_v1,
    load_v2,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from .timing import (
    LogicalClock,
    PerfClock,
    QUERY_COST_MODEL,
    StageRecorder,
    estimate_query_latency_ns,
    logical_cost,
)
from .util import (
    atomic_write_json,
    canonical_json,
    read_json,
    sha256_bytes,
    sha256_json,
)


DEFAULT_QUERY_IDS = ["r0001", "r0009", "r0021", "r0030", "r0032"]


def _capture_error(name: str, expected_code: str, callback: Callable[[], Any]) -> dict[str, Any]:
    try:
        callback()
    except EngineError as error:
        return {
            "case": name,
            "expected_code": expected_code,
            "observed_code": error.code,
            "code_matches": error.code == expected_code,
            "message": error.message,
            "detail": copy.deepcopy(error.detail),
            "completed_steps": list(error.completed_steps),
            "handled": True,
            "fallback": "rejected_before_commit",
        }
    except Exception as error:
        return {
            "case": name,
            "expected_code": expected_code,
            "observed_code": E_INTERNAL_ERROR,
            "code_matches": False,
            "message": "unhandled exception was captured by the diagnostic boundary",
            "detail": {"exception_type": type(error).__name__, "exception": str(error)},
            "completed_steps": [],
            "handled": False,
            "fallback": "rejected_and_recorded_instead_of_swallowed",
        }
    return {
        "case": name,
        "expected_code": expected_code,
        "observed_code": None,
        "code_matches": False,
        "message": "invalid case unexpectedly succeeded",
        "detail": {},
        "completed_steps": [],
        "handled": False,
        "fallback": "case_failed_validation_expectation",
    }


def _selection_brief(computation: Computation) -> dict[str, Any]:
    return {
        "selection_key": computation.selection_key,
        "matrix_digest": computation.matrix_digest,
        "retained_feature_ids": list(computation.retained_feature_ids),
        "scores": list(computation.feature_scores),
        "retained_flags": list(computation.retained_flags),
        "feature_ids": computation.plan.ids,
    }


def _reuse_summary(computation: Computation) -> dict[str, Any]:
    return {
        "reused_entry_count": len(computation.reused_feature_keys),
        "computed_entry_count": len(computation.computed_feature_keys),
        "reused_row_ids": list(computation.reused_row_ids),
        "recomputed_row_ids": list(computation.recomputed_row_ids),
        "affected_feature_ids": list(computation.affected_feature_ids),
        "reused_entry_keys": list(computation.reused_feature_keys),
        "computed_entry_keys": list(computation.computed_feature_keys),
        "reused_entry_key_digest": sha256_json(list(computation.reused_feature_keys)),
        "computed_entry_key_digest": sha256_json(list(computation.computed_feature_keys)),
    }


def _query_brief(response: dict[str, Any], computation: Computation) -> dict[str, Any]:
    matches = canonical_json(response["record"]) == canonical_json(computation.record_for(response["row_id"]))
    return {
        "row_id": response["row_id"],
        "matches_batch_record": matches,
        "record": response["record"],
        "retained_projection": response["retained_projection"],
        "latency": response["latency"],
    }


def _checkpoint_brief(result: dict[str, Any]) -> dict[str, Any]:
    computation = result.get("computation")
    brief = {
        "status": result["status"],
        "reason": result["reason"],
        "resume_reason": result["resume_reason"],
        "checkpoint_version": result["checkpoint_version"],
        "checkpoint_versions": list(result["checkpoint_versions"]),
        "committed_batches_reused": result["committed_batches_reused"],
        "rows_reused_from_checkpoint": result["rows_reused_from_checkpoint"],
        "recomputed_row_ids": list(result["recomputed_row_ids"]),
        "completed_batches_this_run": result["completed_batches_this_run"],
        "row_cursor": result["row_cursor"],
        "total_rows": result["total_rows"],
        "uncommitted_state_discarded": result["uncommitted_state_discarded"],
        "degradations": list(result["degradations"]),
        "selection_available": result["selection_available"],
    }
    if isinstance(computation, Computation):
        brief["selection"] = _selection_brief(computation)
    return brief


def _config_report(
    v1_config: FeatureConfig,
    v2_config: FeatureConfig,
    v1_path: Path,
    v2_path: Path,
) -> dict[str, Any]:
    return {
        "v1": {
            "path": str(v1_path),
            "schema": v1_config.schema,
            "snapshot_id": v1_config.snapshot_id,
            "snapshot_version": v1_config.snapshot_version,
            "semantic_digest": v1_config.semantic_digest,
            "effective_at_sec": round(v1_config.effective_at_sec, 9),
            "completion_sources": copy.deepcopy(v1_config.completion_sources),
        },
        "v2": {
            "path": str(v2_path),
            "schema": v2_config.schema,
            "snapshot_id": v2_config.snapshot_id,
            "snapshot_version": v2_config.snapshot_version,
            "semantic_digest": v2_config.semantic_digest,
            "config_digest": sha256_json(v2_config.to_v2_parts()),
            "effective_at_ms": v2_config.effective_at_ms,
            "fill_sources": copy.deepcopy(v2_config.completion_sources),
        },
        "semantically_equal": configs_semantically_equal(v1_config, v2_config),
    }


def _measure_stage(
    recorder: StageRecorder,
    stage: str,
    *,
    work_units: int,
    unit_kind: str,
    nanos_per_unit: int,
    callback: Callable[[], Any],
) -> Any:
    started = time.perf_counter_ns()
    try:
        result = callback()
        return result
    finally:
        actual_ns = time.perf_counter_ns() - started
        recorder.record(
            stage,
            work_units=work_units,
            unit_kind=unit_kind,
            actual_ns=actual_ns,
            logical_cost_ns=logical_cost(work_units, nanos_per_unit),
        )


def _incremental_case(
    kind: str,
    before: Computation,
    after: Computation,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "kind": kind,
        "selection_key_before": before.selection_key,
        "selection_key_after": after.selection_key,
        "matrix_digest_before": before.matrix_digest,
        "matrix_digest_after": after.matrix_digest,
        "changed_score_feature_ids": compare_score_sets(before, after),
        "retained_feature_ids": list(after.retained_feature_ids),
        "cache_reuse": _reuse_summary(after),
    }
    if extra:
        payload.update(copy.deepcopy(extra))
    return payload


def _query_set(
    cache: CacheStore,
    query_ids: list[str],
    *,
    clock_factory: Callable[[], Any],
) -> list[dict[str, Any]]:
    service = QueryService(cache)
    return [
        service.query_row(row_id, clock=clock_factory())
        for row_id in query_ids
    ]


def run_canonical(
    root: Path,
    *,
    seed: int = 20261126,
    query_ids: list[str] | None = None,
    report_path: Path | None = None,
    telemetry_path: Path | None = None,
    generate_if_missing: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(root)
    query_ids = list(query_ids or DEFAULT_QUERY_IDS)
    work_dir = root / "work"
    artifact_dir = root / "artifacts"
    work_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_path = artifact_dir / "run_report.json" if report_path is None else Path(report_path)
    telemetry_path = artifact_dir / "run_telemetry.json" if telemetry_path is None else Path(telemetry_path)

    v1_path = root / "configs" / "v1.json"
    v2_path = root / "configs" / "v2"
    data_path = root / "data" / "sample.csv"
    meta_path = root / "data" / "sample.meta.json"
    stored_seed = None
    if meta_path.exists():
        try:
            stored_seed = int(read_json(meta_path).get("seed"))
        except Exception:
            stored_seed = None
    needs_prepare = (
        not v1_path.exists()
        or not v2_path.exists()
        or not data_path.exists()
        or (stored_seed is not None and stored_seed != seed)
    )
    if needs_prepare:
        if not generate_if_missing:
            raise EngineError(
                E_PARAM_INVALID,
                "workspace inputs are missing or were generated with a different seed",
                {
                    "field": "seed",
                    "requested_seed": seed,
                    "stored_seed": stored_seed,
                    "generate_if_missing": False,
                },
            )
        prepare_workspace(root, seed=seed)

    recorder = StageRecorder(clock=LogicalClock())
    root_iterations = 1

    def load_inputs() -> tuple[FeatureConfig, FeatureConfig, list[dict[str, Any]]]:
        v1 = load_v1(v1_path, require_checksum=True)
        v2 = load_v2(v2_path, require_checksum=True)
        rows = read_rows_csv(data_path, v2)
        return v1, v2, rows

    v1_config, v2_config, rows = _measure_stage(
        recorder,
        "load_snapshots_and_input",
        work_units=3,
        unit_kind="snapshot_and_input_loads",
        nanos_per_unit=50_000,
        callback=load_inputs,
    )
    input_digest = rows_digest(rows)

    v1_baseline = _measure_stage(
        recorder,
        "v1_baseline_encoding_selection",
        work_units=len(rows) * 25,
        unit_kind="encoded_cells",
        nanos_per_unit=400,
        callback=lambda: compute(rows, v1_config),
    )
    v2_baseline = _measure_stage(
        recorder,
        "v2_baseline_encoding_selection",
        work_units=len(rows) * len(v2_config.categorical),
        unit_kind="encoded_cells",
        nanos_per_unit=400,
        callback=lambda: compute(rows, v2_config),
    )
    migration_equivalence = {
        "v1_selection_key": v1_baseline.selection_key,
        "v2_selection_key": v2_baseline.selection_key,
        "selection_equal": v1_baseline.selection_key == v2_baseline.selection_key,
        "matrix_equal": v1_baseline.matrix_digest == v2_baseline.matrix_digest,
        "retained_equal": v1_baseline.retained_feature_ids == v2_baseline.retained_feature_ids,
    }

    roundtrip_v1_path = work_dir / "roundtrip-v1.json"
    roundtrip_v1_config, _ = migrate_v2_to_v1(
        v2_path,
        roundtrip_v1_path,
        target_version=2,
        checksum=None,
    )
    roundtrip_v1_baseline = compute(rows, roundtrip_v1_config)
    roundtrip_v2_path = work_dir / "roundtrip-v2"
    roundtrip_v2_config, _ = migrate_v1_to_v2(
        roundtrip_v1_path,
        roundtrip_v2_path,
        target_version=2,
        checksum=None,
    )
    roundtrip_v2_baseline = compute(rows, roundtrip_v2_config)
    migration_equivalence["roundtrip_v2_to_v1"] = {
        "semantic_equal": configs_semantically_equal(v2_config, roundtrip_v1_config),
        "selection_key": roundtrip_v1_baseline.selection_key,
        "selection_equal": roundtrip_v1_baseline.selection_key == v2_baseline.selection_key,
    }
    migration_equivalence["roundtrip_v1_to_v2"] = {
        "semantic_equal": configs_semantically_equal(v2_config, roundtrip_v2_config),
        "selection_key": roundtrip_v2_baseline.selection_key,
        "selection_equal": roundtrip_v2_baseline.selection_key == v2_baseline.selection_key,
    }

    run_cache_path = work_dir / "run-cache.sqlite"
    cache = CacheStore(run_cache_path, v2_config.limits["max_cache_entries"])
    cache.reset()

    baseline = _measure_stage(
        recorder,
        "baseline_cached_encoding_selection",
        work_units=len(rows) * len(v2_baseline.plan.ids),
        unit_kind="encoded_cells",
        nanos_per_unit=400,
        callback=lambda: compute(rows, v2_config, cache=cache),
    )
    baseline_commit = _measure_stage(
        recorder,
        "baseline_cache_commit",
        work_units=len(rows) * len(baseline.plan.ids),
        unit_kind="cache_entries",
        nanos_per_unit=100,
        callback=lambda: cache.commit(baseline, input_digest=input_digest, config_digest=v2_config.semantic_digest),
    )

    stale_rejections: list[dict[str, Any]] = []

    def stale_rejection(previous_key: str, generation: int) -> None:
        try:
            cache.load_selection(previous_key)
        except EngineError as error:
            stale_rejections.append(
                {
                    "selection_key": previous_key,
                    "current_generation": generation,
                    "code": error.code,
                    "detail": copy.deepcopy(error.detail),
                }
            )

    corrected_rows = apply_row_correction(
        rows,
        "r0030",
        numeric_updates={"score": 812.125},
        categorical_updates={"region": "west"},
    )
    correction_computation = _measure_stage(
        recorder,
        "incremental_row_correction",
        work_units=len(rows) * len(baseline.plan.ids),
        unit_kind="dependency_checked_cells",
        nanos_per_unit=220,
        callback=lambda: compute(corrected_rows, v2_config, cache=cache, previous=baseline),
    )
    correction_commit = cache.commit(
        correction_computation,
        input_digest=rows_digest(corrected_rows),
        config_digest=v2_config.semantic_digest,
    )
    stale_rejection(baseline.selection_key, correction_commit["generation"])
    correction_report = _incremental_case(
        "row_value_correction",
        baseline,
        correction_computation,
        extra={
            "row_id": "r0030",
            "numeric_updates": {"score": 812.125},
            "categorical_updates": {"region": "west"},
        },
    )

    codec_config = change_categorical_codec(v2_config, "channel", "target")
    codec_computation = _measure_stage(
        recorder,
        "incremental_categorical_codec_switch",
        work_units=len(rows) * len(corrected_rows[0]["categoricals"]),
        unit_kind="dependency_checked_rows",
        nanos_per_unit=2_000,
        callback=lambda: compute(corrected_rows, codec_config, cache=cache, previous=correction_computation),
    )
    codec_commit = cache.commit(
        codec_computation,
        input_digest=rows_digest(corrected_rows),
        config_digest=codec_config.semantic_digest,
    )
    stale_rejection(correction_computation.selection_key, codec_commit["generation"])
    codec_report = _incremental_case(
        "categorical_encoding_switch",
        correction_computation,
        codec_computation,
        extra={"column": "channel", "from_codec": "one_hot", "to_codec": "target"},
    )

    added_cross_config = add_cross_slot(
        codec_config,
        {
            "slot": "region__channel",
            "columns": ["region", "channel"],
            "codec": "one_hot",
            "prior_weight": 5.0,
        },
    )
    added_cross_computation = _measure_stage(
        recorder,
        "incremental_cross_slot_add",
        work_units=len(rows) * 12,
        unit_kind="new_cross_cells",
        nanos_per_unit=700,
        callback=lambda: compute(corrected_rows, added_cross_config, cache=cache, previous=codec_computation),
    )
    added_cross_commit = cache.commit(
        added_cross_computation,
        input_digest=rows_digest(corrected_rows),
        config_digest=added_cross_config.semantic_digest,
    )
    stale_rejection(codec_computation.selection_key, added_cross_commit["generation"])
    cross_add_report = _incremental_case(
        "cross_slot_add",
        codec_computation,
        added_cross_computation,
        extra={"slot": "region__channel", "codec": "one_hot", "source_columns": ["region", "channel"]},
    )

    removed_cross_config = remove_cross_slot(added_cross_config, "income__score")
    removed_cross_computation = _measure_stage(
        recorder,
        "incremental_cross_slot_remove",
        work_units=len(rows),
        unit_kind="dependency_checked_rows",
        nanos_per_unit=2_000,
        callback=lambda: compute(corrected_rows, removed_cross_config, cache=cache, previous=added_cross_computation),
    )
    removed_cross_commit = cache.commit(
        removed_cross_computation,
        input_digest=rows_digest(corrected_rows),
        config_digest=removed_cross_config.semantic_digest,
    )
    stale_rejection(added_cross_computation.selection_key, removed_cross_commit["generation"])
    cross_remove_report = _incremental_case(
        "cross_slot_remove",
        added_cross_computation,
        removed_cross_computation,
        extra={"slot": "income__score", "codec": "product"},
    )
    final_computation = removed_cross_computation

    serial_queries = _measure_stage(
        recorder,
        "interactive_row_queries",
        work_units=len(query_ids),
        unit_kind="queries",
        nanos_per_unit=250_000,
        callback=lambda: _query_set(cache, query_ids, clock_factory=LogicalClock),
    )
    query_report = [_query_brief(response, final_computation) for response in serial_queries]
    query_consistency = all(item["matches_batch_record"] for item in query_report)

    max_workers = int(removed_cross_config.limits["max_query_workers"])
    requested_workers = 4
    effective_parallel_workers = min(max_workers, len(query_ids))
    parallel_queries, parallel_degradation = _measure_stage(
        recorder,
        "parallel_query_execution",
        work_units=len(query_ids),
        unit_kind="queries",
        nanos_per_unit=400_000,
        callback=lambda: query_many(
            run_cache_path,
            removed_cross_config.limits["max_cache_entries"],
            query_ids,
            requested_workers=effective_parallel_workers,
            max_workers=max_workers,
            on_limit=removed_cross_config.resource_policy["on_query_workers"],
            clock_mode="logical",
        ),
    )
    parallel_result_digest = sha256_json(parallel_queries)
    serial_result_digest = sha256_json(serial_queries)
    if requested_workers > max_workers:
        if removed_cross_config.resource_policy["on_query_workers"] == "degrade_serial":
            worker_degradation = {
                "category": "query_workers_serialized",
                "resource": "query_workers",
                "requested": requested_workers,
                "applied_limit": max_workers,
                "effective_mode": "serial",
                "result_unchanged": True,
            }
            degraded_queries, query_many_degradation = query_many(
                run_cache_path,
                removed_cross_config.limits["max_cache_entries"],
                query_ids,
                requested_workers=requested_workers,
                max_workers=max_workers,
                on_limit=removed_cross_config.resource_policy["on_query_workers"],
                clock_mode="logical",
            )
            degraded_query_digest = sha256_json(degraded_queries)
            worker_degradation["result_digest"] = degraded_query_digest
            worker_degradation["serial_result_digest"] = serial_result_digest
            worker_degradation["results_equal"] = degraded_query_digest == serial_result_digest
            if query_many_degradation is not None:
                worker_degradation["enforced_by"] = "query_many"
        else:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "requested query workers exceed max_query_workers",
                {
                    "resource": "query_workers",
                    "limit": max_workers,
                    "requested": requested_workers,
                },
            )
    else:
        worker_degradation = None
    parallel_query_equivalence = parallel_result_digest == serial_result_digest
    worker_query_equivalence = (
        True if worker_degradation is None else bool(worker_degradation["results_equal"])
    )

    forced_checkpoint_path = work_dir / "recovery-forced.sqlite"
    graceful_checkpoint_path = work_dir / "recovery-graceful.sqlite"

    def run_forced_then_resume() -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            run_checkpointed_batches(
                rows,
                v2_config,
                forced_checkpoint_path,
                requested_batch_size=12,
                reset=True,
                hard_stop_after_batches=2,
            )
        except SimulatedHardStop as stop:
            stopped_version = stop.checkpoint_version
        else:
            raise RuntimeError("forced-stop simulation did not stop")
        resumed = run_checkpointed_batches(
            rows,
            v2_config,
            forced_checkpoint_path,
            requested_batch_size=12,
            reset=False,
        )
        return {"simulated_stop_version": stopped_version}, resumed

    forced_stop, forced_resumed = _measure_stage(
        recorder,
        "checkpoint_forced_recovery",
        work_units=len(rows) * 2,
        unit_kind="checkpointed_rows",
        nanos_per_unit=50_000,
        callback=run_forced_then_resume,
    )
    forced_selection = forced_resumed["computation"]
    forced_equivalence = {
        "selection_equal_to_baseline": forced_selection.selection_key == v2_baseline.selection_key,
        "matrix_equal_to_baseline": forced_selection.matrix_digest == v2_baseline.matrix_digest,
    }

    graceful_first = _measure_stage(
        recorder,
        "checkpoint_graceful_stop",
        work_units=len(rows),
        unit_kind="checkpointed_rows",
        nanos_per_unit=30_000,
        callback=lambda: run_checkpointed_batches(
            rows,
            v2_config,
            graceful_checkpoint_path,
            requested_batch_size=12,
            reset=True,
            graceful_stop_after_batches=2,
        ),
    )
    graceful_resumed = _measure_stage(
        recorder,
        "checkpoint_graceful_resume",
        work_units=len(rows),
        unit_kind="checkpointed_rows",
        nanos_per_unit=30_000,
        callback=lambda: run_checkpointed_batches(
            rows,
            v2_config,
            graceful_checkpoint_path,
            requested_batch_size=12,
            reset=False,
        ),
    )
    graceful_equivalence = {
        "selection_equal_to_baseline": graceful_resumed["computation"].selection_key == v2_baseline.selection_key,
        "matrix_equal_to_baseline": graceful_resumed["computation"].matrix_digest == v2_baseline.matrix_digest,
    }

    diagnostic_cache_path = work_dir / "diagnostic-cache.sqlite"
    diagnostic_cache = CacheStore(diagnostic_cache_path, v2_config.limits["max_cache_entries"])
    diagnostic_cache.reset()
    diagnostic_cache.commit(
        baseline,
        input_digest=input_digest,
        config_digest=v2_config.semantic_digest,
    )

    def duplicate_row_case() -> None:
        invalid = copy.deepcopy(rows)
        duplicate = copy.deepcopy(invalid[0])
        duplicate["row_id"] = invalid[0]["row_id"]
        invalid.append(duplicate)
        compute(invalid, v2_config)

    def nonfinite_case() -> None:
        invalid = copy.deepcopy(rows)
        invalid[0]["numerics"]["score"] = float("nan")
        compute(invalid, v2_config)

    def category_case() -> None:
        invalid = copy.deepcopy(rows)
        invalid[0]["categoricals"]["region"] = "outside"
        compute(invalid, v2_config)

    def missing_cross_case() -> None:
        add_cross_slot(
            v2_config,
            {
                "slot": "bad__missing",
                "columns": ["region", "not_a_column"],
                "codec": "one_hot",
                "prior_weight": 5.0,
            },
        )

    def resource_case() -> None:
        constrained = replace_config(v2_config, limits={**v2_config.limits, "max_resident_bytes": 1})
        compute(rows, constrained)

    def timeout_case() -> None:
        QueryService(cache).query_row("r0001", timeout_ms=0, clock=LogicalClock())

    def stale_case() -> None:
        cache.load_selection(baseline.selection_key)

    def stage_busy_case() -> None:
        with cache.held_stage():
            cache.commit(
                final_computation,
                input_digest=rows_digest(corrected_rows),
                config_digest=removed_cross_config.semantic_digest,
            )

    def encoding_cache_case() -> None:
        first_row_id = baseline.rows[0]["row_id"]
        key = sorted(baseline.feature_keys[first_row_id])[0]
        connection = sqlite3.connect(str(diagnostic_cache_path), timeout=0)
        try:
            connection.execute(
                "UPDATE encoding_cache SET value_json='{not-json' WHERE cache_key=?",
                (key,),
            )
            connection.commit()
        finally:
            connection.close()
        diagnostic_cache.fetch_encoding_values([key])

    checksum_good = (v1_path.with_name(v1_path.name + ".sha256")).read_text(encoding="utf-8").strip()

    def snapshot_checksum_case() -> None:
        bad_path = work_dir / "diagnostic-bad-checksum-v1.json"
        bad_path.write_bytes(v1_path.read_bytes() + b" ")
        load_v1(bad_path, checksum=checksum_good, require_checksum=True)

    def snapshot_parse_case() -> None:
        bad_path = work_dir / "diagnostic-unparseable-v1.json"
        raw = read_json(v1_path)
        raw["selector"]["target_k"] = "not-an-integer"
        data = canonical_json(raw, newline=True).encode("utf-8")
        bad_path.write_bytes(data)
        bad_path.with_name(bad_path.name + ".sha256").write_text(
            sha256_bytes(data) + "\n",
            encoding="utf-8",
        )
        load_v1(bad_path, require_checksum=True)

    def snapshot_rollback_case() -> None:
        migrate_v2_to_v1(
            v2_path,
            work_dir / "diagnostic-rollback-v1.json",
            target_version=1,
            checksum=None,
        )

    diagnostics = _measure_stage(
        recorder,
        "injected_validation_matrix",
        work_units=11,
        unit_kind="validation_cases",
        nanos_per_unit=100_000,
        callback=lambda: [
            _capture_error("duplicate_row_id", E_BAD_INPUT, duplicate_row_case),
            _capture_error("nonfinite_numeric", E_BAD_INPUT, nonfinite_case),
            _capture_error("category_out_of_range", E_ENCODING_ERROR, category_case),
            _capture_error("cross_slot_missing", E_PARAM_INVALID, missing_cross_case),
            _capture_error("resident_matrix_limit", E_RESOURCE_LIMIT, resource_case),
            _capture_error("query_latency_budget", E_LATENCY_BUDGET, timeout_case),
            _capture_error("stale_selection_key", E_STALE_RESULT, stale_case),
            _capture_error("stage_commit_busy", E_STAGE_BUSY, stage_busy_case),
            _capture_error("encoding_cache_unparseable", E_ENCODING_ERROR, encoding_cache_case),
            _capture_error("snapshot_checksum_mismatch", E_SNAPSHOT_ROLLBACK, snapshot_checksum_case),
            _capture_error("snapshot_field_unparseable", E_SNAPSHOT_ROLLBACK, snapshot_parse_case),
            _capture_error("snapshot_version_rollback", E_SNAPSHOT_ROLLBACK, snapshot_rollback_case),
        ],
    )

    final_query_latencies = [item["latency"] for item in serial_queries]
    stage_work = sum(event["work_units"] for event in recorder.stages)

    report = {
        "report_schema": "feature_diag_report/v1",
        "engine_version": "0.1.0",
        "workspace": {
            "data": "data/sample.csv",
            "config_v1": "configs/v1.json",
            "config_v2": "configs/v2",
            "seed": seed,
            "clock_policy": {
                "canonical_report": "logical_work_units",
                "actual_measurement": "perf_counter_ns",
                "canonical_report_is_byte_reproducible": True,
                "actual_timing_artifact": str(telemetry_path.relative_to(root)),
            },
        },
        "input": {
            "row_count": len(rows),
            "input_digest": input_digest,
            "fit_rows": v2_config.fit_rows,
            "sort_key": "row_id_ascending",
        },
        "config": _config_report(
            v1_config,
            v2_config,
            Path("configs/v1.json"),
            Path("configs/v2"),
        ),
        "migration": migration_equivalence,
        "batch": final_computation.summary(),
        "baseline": {
            "selection_key": baseline.selection_key,
            "matrix_digest": baseline.matrix_digest,
            "retained_feature_ids": list(baseline.retained_feature_ids),
            "cache_commit": baseline_commit,
        },
        "incremental": {
            "row_correction": correction_report,
            "categorical_codec_switch": codec_report,
            "cross_slot_add": cross_add_report,
            "cross_slot_remove": cross_remove_report,
            "stale_result_rejections": stale_rejections,
            "final_selection_key": final_computation.selection_key,
        },
        "queries": {
            "rows": query_report,
            "all_match_batch": query_consistency,
            "latency_measurement_rule": {
                "clock": "logical_work_units",
                "estimate_formula": {
                    **QUERY_COST_MODEL,
                    "expression": "base_ns + per_row_ns*row_count + per_feature_ns*feature_count + per_retained_feature_ns*retained_feature_count",
                },
                "completed_steps": [
                    "validate_request",
                    "load_current_selection",
                    "locate_row_record",
                    "decode_encoding_vector",
                    "project_retained_features",
                ],
                "latencies": final_query_latencies,
            },
            "parallelism": {
                "configured_max_query_workers": max_workers,
                "requested_query_workers": requested_workers,
                "parallel_execution": {
                    "workers": effective_parallel_workers,
                    "degradation": parallel_degradation,
                    "result_digest": parallel_result_digest,
                    "serial_result_digest": serial_result_digest,
                    "results_equal": parallel_result_digest == serial_result_digest,
                },
                "degradation": worker_degradation,
            },
        },
        "recovery": {
            "forced_termination": {
                "simulated_stop_version": forced_stop["simulated_stop_version"],
                "resume": _checkpoint_brief(forced_resumed),
                "selection_equivalence": forced_equivalence,
            },
            "graceful_stop": {
                "stopped": _checkpoint_brief(graceful_first),
                "resume": _checkpoint_brief(graceful_resumed),
                "selection_equivalence": graceful_equivalence,
            },
        },
        "diagnostics": diagnostics,
        "rejections": {
            "stale_result_rejections": stale_rejections,
            "diagnostic_rejections": [
                {
                    "case": item["case"],
                    "code": item["observed_code"],
                    "handled": item["handled"],
                    "fallback": item["fallback"],
                    "detail": item["detail"],
                }
                for item in diagnostics
            ],
        },
        "degradations": [
            item
            for item in [
                worker_degradation,
                *forced_resumed["degradations"],
                *graceful_resumed["degradations"],
            ]
            if item is not None
        ],
        "business_codes": sorted(
            {
                item["observed_code"]
                for item in diagnostics
                if item["observed_code"] is not None
            }
            | {item["code"] for item in stale_rejections}
        ),
        "timing": {
            "clock": "logical_work_units",
            "rule": "deterministic work counters multiplied by fixed per-unit logical nanoseconds",
            "total_work_units": stage_work,
            "stages": recorder.deterministic_view(),
        },
        "cache": {
            "final_generation": removed_cross_commit["generation"],
            "final_entry_count": removed_cross_commit["cache_entries"],
            "final_selection_payload_checksum": removed_cross_commit["selection_payload_checksum"],
            "content_addressing": "sha256(row_id, source_values, feature_spec, fit_context_digest)",
        },
        "summary": {
            "ok": all(item["code_matches"] and item["handled"] for item in diagnostics)
            and query_consistency
            and parallel_query_equivalence
            and worker_query_equivalence
            and migration_equivalence["selection_equal"]
            and forced_equivalence["selection_equal_to_baseline"]
            and graceful_equivalence["selection_equal_to_baseline"],
            "row_count": len(rows),
            "encoded_feature_count": len(final_computation.plan.ids),
            "selected_feature_count": len(final_computation.retained_feature_ids),
            "final_selection_key": final_computation.selection_key,
            "diagnostic_case_count": len(diagnostics),
        },
    }

    report_bytes = canonical_json(report, newline=True).encode("utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(report_bytes)
    report_sha = sha256_bytes(report_bytes)

    telemetry_queries = [
        QueryService(cache).query_row(row_id, clock=PerfClock())
        for row_id in query_ids
    ]
    telemetry = {
        "telemetry_schema": "feature_diag_telemetry/v1",
        "canonical_report_sha256": report_sha,
        "clock": "perf_counter_ns",
        "stages": recorder.actual_view(),
        "query_latencies": [
            {
                "row_id": item["row_id"],
                "actual_ns": item["latency"]["actual_ns"],
                "estimated_ns": item["latency"]["estimated_ns"],
                "clock": item["latency"]["clock"],
            }
            for item in telemetry_queries
        ],
    }
    atomic_write_json(telemetry_path, telemetry)
    return report, {
        "report_path": str(report_path),
        "telemetry_path": str(telemetry_path),
        "report_sha256": report_sha,
        "row_count": len(rows),
        "final_selection_key": final_computation.selection_key,
    }
