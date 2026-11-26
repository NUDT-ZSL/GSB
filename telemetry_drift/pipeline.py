"""End-to-end deterministic drift diagnostics pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .alignment import AlignmentResult, align_observations, load_observations
from .cache import (
    ContentAddressedCache,
    DependencyGraph,
    VariableRevision,
    build_run_cache_key,
    build_window_cache_key,
    snapshot_cache_version,
    window_content_hash,
)
from .canonical import sha256_value
from .checkpoint import StageStateMachine
from .config import PipelineConfig
from .errors import DriftError, E_CACHE_CORRUPT
from .ledger import build_ledger_entries
from .retraining import RetrainOutcome, retrain_closures
from .snapshots import PredictorModel, SnapshotBundle, read_snapshot
from .windows import (
    WindowEvidence,
    WindowSpec,
    build_window_evidence,
    build_window_evidence_for_specs,
    build_window_specs,
    window_evidence_from_cache,
)


def _coerce_config(value: str | Path | PipelineConfig) -> PipelineConfig:
    return value if isinstance(value, PipelineConfig) else PipelineConfig.load(value)


def _coerce_records(
    value: str | Path | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else load_observations(value)


def _coerce_snapshot(value: str | Path | SnapshotBundle) -> SnapshotBundle:
    return value if isinstance(value, SnapshotBundle) else read_snapshot(value)


def _commit_stage(
    checkpoint_path: str | Path | None,
    lock_path: str | Path | None,
    stage: str,
    records: Iterable[str],
) -> dict[str, Any] | None:
    if checkpoint_path is None:
        return None
    state = StageStateMachine(checkpoint_path, lock_path=lock_path)
    return state.commit_stage(stage, records=records)


def _window_key(
    aligned: AlignmentResult,
    config: PipelineConfig,
    snapshot: SnapshotBundle,
    spec: WindowSpec,
) -> str:
    return build_window_cache_key(
        input_variable_content_hash=window_content_hash(aligned, config, spec),
        predictor_snapshot_version=snapshot_cache_version(snapshot),
        residual_window_range=(spec.start_index, spec.end_index),
        recycle_rate_threshold=config.predictor.recycle_rate_threshold,
        retrain_config_digest=config.retrain_digest(),
    )


def _load_cached_evidence(
    payload: dict[str, Any],
    config: PipelineConfig,
) -> WindowEvidence:
    stored_digest = payload.get("digest")
    body = {key: value for key, value in payload.items() if key != "digest"}
    if stored_digest != sha256_value(body):
        raise DriftError(
            E_CACHE_CORRUPT,
            "cached window evidence digest mismatch",
            window=payload.get("window_id"),
        )
    return window_evidence_from_cache(payload, config)


def _semantic_payload(
    *,
    config: PipelineConfig,
    aligned: AlignmentResult,
    windows: list[WindowEvidence],
    outcome: RetrainOutcome,
    ledger_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "telemetry-drift-semantic-result-v1",
        "seed": config.seed,
        "config_digest": config.digest(),
        "alignment_digest": aligned.digest,
        "window_evidence": [
            {
                "window_id": window.window_id,
                "digest": window.digest,
                "drift_score": window.statistics.drift_score,
                "input_amplitude": window.statistics.input_amplitude,
                "drift": window.decision.drift,
                "predictions": list(window.predictions),
            }
            for window in windows
        ],
        "closures": [closure.to_dict() for closure in outcome.closures],
        "retrained_models": [
            retrain.model_after.to_dict() for retrain in outcome.retrains
        ],
        "window_predictions": {
            key: list(value)
            for key, value in sorted(outcome.window_predictions.items())
        },
        "reused_windows": list(outcome.reused_windows),
        "recomputed_windows": list(outcome.recomputed_windows),
        "ledger_digests": [
            entry["result_digest"] for entry in ledger_entries
        ],
        "business_codes": sorted(
            {
                code
                for window in windows
                for code in window.decision.business_codes
            }
            | set(outcome.business_codes)
            | {
                code
                for entry in ledger_entries
                for code in entry["business_codes"]
            }
        ),
    }


def _compile_result(
    *,
    config: PipelineConfig,
    snapshot: SnapshotBundle,
    aligned: AlignmentResult,
    windows: list[WindowEvidence],
    outcome: RetrainOutcome,
    ledger_entries: list[dict[str, Any]],
    cache_info: dict[str, Any],
    checkpoint: str | None,
) -> dict[str, Any]:
    semantic = _semantic_payload(
        config=config,
        aligned=aligned,
        windows=windows,
        outcome=outcome,
        ledger_entries=ledger_entries,
    )
    semantic_digest = sha256_value(semantic)
    return {
        "schema": "telemetry-drift-run-v1",
        "seed": config.seed,
        "config": config.to_dict(),
        "config_digest": config.digest(),
        "snapshot": {
            "version": snapshot.version,
            "current_model_id": snapshot.current_model_id,
            "source_path": snapshot.source_path,
            "model": snapshot.current.to_dict(),
        },
        "alignment": aligned.to_dict(),
        "windows": [window.to_dict() for window in windows],
        "closures": [closure.to_dict() for closure in outcome.closures],
        "retrain": outcome.to_dict(),
        "ledger_entries": ledger_entries,
        "summary": {
            "window_count": len(windows),
            "drift_event_count": sum(
                1 for window in windows if window.decision.drift
            ),
            "closure_count": len(outcome.closures),
            "retrained_closure_count": len(outcome.retrains),
            "recomputed_window_count": len(cache_info["recomputed_windows"]),
            "reused_window_count": len(cache_info["reused_windows"]),
        },
        "cache": cache_info,
        "checkpoint": checkpoint,
        "comparison_digest": semantic_digest,
    }


def run_pipeline(
    config: str | Path | PipelineConfig,
    observations: str | Path | list[dict[str, Any]],
    snapshot: str | Path | SnapshotBundle,
    *,
    cache_dir: str | Path | None = None,
    use_cache: bool = True,
    changes: dict[str, Any] | None = None,
    checkpoint_path: str | Path | None = None,
    lock_path: str | Path | None = None,
) -> dict[str, Any]:
    cfg = _coerce_config(config)
    records = _coerce_records(observations)
    snap = _coerce_snapshot(snapshot)
    changes = changes or {}
    _commit_stage(
        checkpoint_path,
        lock_path,
        "ingest",
        (f"records:{len(records)}",),
    )
    aligned = align_observations(records, cfg)
    _commit_stage(
        checkpoint_path,
        lock_path,
        "align",
        (f"alignment:{aligned.digest}",),
    )
    specs = build_window_specs(aligned, cfg)
    keys = [_window_key(aligned, cfg, snap, spec) for spec in specs]
    run_key = build_run_cache_key(
        config=cfg,
        snapshot=snap,
        window_keys=keys,
    )
    cache = ContentAddressedCache(cache_dir) if cache_dir is not None else None

    if cache is not None and use_cache and not changes and cache.has(run_key):
        cached = cache.get(run_key)
        all_windows = [str(item["window_id"]) for item in cached["windows"]]
        cached["cache"] = {
            "run_key": run_key,
            "run_hit": True,
            "recomputed_windows": [],
            "reused_windows": all_windows,
            "impact": None,
        }
        cached["summary"]["recomputed_window_count"] = 0
        cached["summary"]["reused_window_count"] = len(all_windows)
        cached["checkpoint"] = str(checkpoint_path) if checkpoint_path else None
        return cached

    revisions = [
        VariableRevision(
            variable=int(item["variable"]),
            start_ms=int(item["start_ms"]),
            end_ms=int(item["end_ms"]),
            reason=str(item.get("reason", "VARIABLE_REVISION")),
        )
        for item in changes.get("revisions", [])
    ]
    for variable in changes.get("changed_variables", []):
        if not any(revision.variable == int(variable) for revision in revisions):
            revisions.append(
                VariableRevision(
                    variable=int(variable),
                    start_ms=aligned.times_ms[0],
                    end_ms=aligned.times_ms[-1],
                )
            )
    graph = DependencyGraph.from_config(cfg, specs)
    impact = graph.propagate(
        changed_variables=changes.get("changed_variables", []),
        changed_predictors=changes.get("changed_predictors", []),
        revisions=revisions,
        window_size_changed=bool(changes.get("window_size_changed", False)),
    )
    affected = set(impact.affected_windows)
    recomputed: set[str] = set()
    reused: set[str] = set()
    windows: list[WindowEvidence] = []
    for spec, key in zip(specs, keys):
        if (
            cache is not None
            and use_cache
            and spec.window_id not in affected
            and cache.has(key)
        ):
            payload = cache.get(key)
            windows.append(_load_cached_evidence(payload, cfg))
            reused.add(spec.window_id)
            continue
        [evidence] = build_window_evidence_for_specs(
            aligned, snap.current, cfg, [spec]
        )
        if cache is not None:
            cache.put(key, evidence.to_dict())
        windows.append(evidence)
        recomputed.add(spec.window_id)

    _commit_stage(
        checkpoint_path,
        lock_path,
        "score",
        tuple(f"window:{window.digest}" for window in windows),
    )
    outcome = retrain_closures(windows, snap.current, cfg)
    recomputed.update(outcome.recomputed_windows)
    reused = {window.window_id for window in windows} - recomputed
    _commit_stage(
        checkpoint_path,
        lock_path,
        "retrain",
        tuple(
            f"closure:{retrain.closure.closure_id}:{retrain.result_digest}"
            for retrain in outcome.retrains
        ),
    )
    ledger_entries = build_ledger_entries(outcome, windows)
    _commit_stage(
        checkpoint_path,
        lock_path,
        "ledger",
        tuple(
            f"ledger:{entry['entry_id']}:{entry['entry_checksum']}"
            for entry in ledger_entries
        ),
    )
    cache_info = {
        "run_key": run_key,
        "run_hit": False,
        "impact": impact.to_dict() if changes else None,
        "recomputed_windows": sorted(recomputed),
        "reused_windows": sorted(reused),
    }
    result = _compile_result(
        config=cfg,
        snapshot=snap,
        aligned=aligned,
        windows=windows,
        outcome=outcome,
        ledger_entries=ledger_entries,
        cache_info=cache_info,
        checkpoint=str(checkpoint_path) if checkpoint_path else None,
    )
    if cache is not None:
        cache_payload = {
            key: value
            for key, value in result.items()
            if key != "cache"
        }
        cache.put(run_key, cache_payload)
    return result


def compare_snapshot_runs(
    config: str | Path | PipelineConfig,
    observations: str | Path | list[dict[str, Any]],
    snapshot_v1: str | Path | SnapshotBundle,
    snapshot_v2: str | Path | SnapshotBundle,
    *,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    first = run_pipeline(
        config,
        observations,
        snapshot_v1,
        cache_dir=None if cache_dir is None else Path(cache_dir) / "v1",
    )
    second = run_pipeline(
        config,
        observations,
        snapshot_v2,
        cache_dir=None if cache_dir is None else Path(cache_dir) / "v2",
    )
    checks = {
        "comparison_digest_equal": (
            first["comparison_digest"] == second["comparison_digest"]
        ),
        "alignment_digest_equal": (
            first["alignment"]["digest"] == second["alignment"]["digest"]
        ),
        "closure_windows_equal": (
            [closure["covered_windows"] for closure in first["closures"]]
            == [closure["covered_windows"] for closure in second["closures"]]
        ),
        "drift_scores_equal": (
            [
                window["statistics"]["drift_score"]
                for window in first["windows"]
            ]
            == [
                window["statistics"]["drift_score"]
                for window in second["windows"]
            ]
        ),
        "window_predictions_equal": (
            first["retrain"]["window_predictions"]
            == second["retrain"]["window_predictions"]
        ),
        "retrained_models_equal": [
            retrain["model_after"]["coefficients"]
            for retrain in first["retrain"]["retrains"]
        ]
        == [
            retrain["model_after"]["coefficients"]
            for retrain in second["retrain"]["retrains"]
        ],
        "ledger_digests_equal": [
            entry["result_digest"] for entry in first["ledger_entries"]
        ]
        == [entry["result_digest"] for entry in second["ledger_entries"]],
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "v1_comparison_digest": first["comparison_digest"],
        "v2_comparison_digest": second["comparison_digest"],
        "v1_summary": first["summary"],
        "v2_summary": second["summary"],
    }

