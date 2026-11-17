from __future__ import annotations

import copy
import time
from typing import Any

from .config import (
    config_hash,
    feature_config_hash,
    feature_projection,
    scoring_projection,
    semantic_config_hash,
)
from .dependencies import affected_summary_outputs
from .features import extract_many
from .io_utils import sha256_obj, sorted_failures
from .plan import apply_batch_to_raw, materialize_final, materialize_prefix
from .samples import data_fingerprint, load_records
from .scoring import score_many
from .snapshot import SnapshotStore
from .summary import compute_summary


LOGICAL_STAGES = ("features", "scoring", "discrimination", "summary")


def _stage_versions(manifest: dict[str, Any]) -> dict[str, int]:
    return {name: int(manifest["stage_versions"][name]) for name in manifest["stage_versions"]}


def _state_from_payloads(
    manifest: dict[str, Any], payloads: dict[str, Any]
) -> dict[str, Any]:
    feature_payload = payloads["features"]
    score_payload = payloads["scores"]
    summary_payload = payloads["summary"]
    raw_entries = feature_payload["raw_entries"]
    loaded = load_records(raw_entries)
    return {
        "manifest": manifest,
        "config": copy.deepcopy(manifest["effective_config"]),
        "raw_entries": copy.deepcopy(raw_entries),
        "loaded": loaded,
        "data_fingerprint": feature_payload["data_fingerprint"],
        "features": copy.deepcopy(feature_payload["entries"]),
        "feature_failures": copy.deepcopy(feature_payload.get("failures", [])),
        "bucket_references": copy.deepcopy(
            feature_payload.get("bucket_references", {})
        ),
        "scores": copy.deepcopy(score_payload["entries"]),
        "score_failures": copy.deepcopy(score_payload.get("failures", [])),
        "summary": copy.deepcopy(summary_payload["data"]),
        "stage_versions": _stage_versions(manifest),
    }


def _build_payloads(
    state: dict[str, Any],
    feature_stage_version: int,
    score_stage_version: int,
    summary_stage_version: int,
) -> dict[str, dict[str, Any]]:
    config = state["config"]
    feature_fingerprint = sha256_obj(
        {
            "feature_config": feature_projection(config),
            "raw_entries": state["raw_entries"],
            "entries": state["features"],
            "failures": state["feature_failures"],
            "bucket_references": state["bucket_references"],
        }
    )
    score_fingerprint = sha256_obj(
        {
            "scoring_config": scoring_projection(config),
            "entries": state["scores"],
            "failures": state["score_failures"],
        }
    )
    return {
        "features": {
            "stage": "feature_extraction",
            "stage_version": feature_stage_version,
            "fingerprint": feature_fingerprint,
            "feature_config_hash": feature_config_hash(config),
            "config": config,
            "data_fingerprint": state["data_fingerprint"],
            "raw_entries": state["raw_entries"],
            "entries": state["features"],
            "failures": state["feature_failures"],
            "bucket_references": state["bucket_references"],
        },
        "scores": {
            "stage": "scoring_discrimination",
            "stage_version": score_stage_version,
            "fingerprint": score_fingerprint,
            "scoring_config": scoring_projection(config),
            "entries": state["scores"],
            "failures": state["score_failures"],
        },
        "summary": {
            "stage": "summary_statistics",
            "stage_version": summary_stage_version,
            "fingerprint": state["summary"]["summary_fingerprint"],
            "data": state["summary"],
        },
    }


def _combine_failures(state: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted_failures(
        state["loaded"]["failures"]
        + state["feature_failures"]
        + state["score_failures"]
    )


def _canonical_result(state: dict[str, Any], batch_ids: list[str]) -> dict[str, Any]:
    pair_ids = sorted(state["loaded"]["valid"])
    return {
        "format_version": 1,
        "config_hash": semantic_config_hash(state["config"]),
        "feature_config_hash": feature_config_hash(state["config"]),
        "data_fingerprint": state["data_fingerprint"],
        "batch_ids": list(batch_ids),
        "valid_records": [
            state["loaded"]["valid"][pair_id] for pair_id in pair_ids
        ],
        "feature_entries": [
            state["features"][pair_id]
            for pair_id in sorted(state["features"])
        ],
        "score_entries": [
            state["scores"][pair_id] for pair_id in sorted(state["scores"])
        ],
        "bucket_references": {
            key: state["bucket_references"][key]
            for key in sorted(state["bucket_references"], key=lambda item: int(item))
        },
        "failures": _combine_failures(state),
        "summary": state["summary"],
    }


def _bucket_refs_for(features: dict[str, Any], pair_ids: set[str]) -> set[int]:
    refs: set[int] = set()
    for pair_id in pair_ids:
        if pair_id in features:
            refs.update(int(item) for item in features[pair_id]["referenced_buckets"])
    return refs


def _same_sequence(left: list[Any], right: list[Any]) -> bool:
    return left == right


def _resume_or_initialize(
    *,
    store: SnapshotStore,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
    dependencies: dict[str, Any],
    crash_after_stage: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    loaded_snapshot = store.load()
    events: list[dict[str, Any]] = []
    recovery: dict[str, Any] = {
        "manifest_present": loaded_snapshot.get("present", False),
        "manifest_status": loaded_snapshot.get("reason"),
        "ignored_orphan_versions": loaded_snapshot.get("orphan_versions", []),
        "stage_hashes_verified": bool(loaded_snapshot.get("valid")),
    }

    if loaded_snapshot.get("valid"):
        manifest = loaded_snapshot["manifest"]
        cursor = int(manifest.get("cursor", -1))
        if 0 <= cursor <= len(plan["batches"]):
            expected = materialize_prefix(base_entries, base_config, plan, cursor)
            prefix_ids = expected["batch_ids"]
            state = _state_from_payloads(manifest, loaded_snapshot["payloads"])
            plan_prefix_matches = (
                manifest.get("completed_batches") == prefix_ids
                and manifest.get("batch_hashes") == expected["batch_hashes"]
            )
            if plan_prefix_matches:
                expected_config_hash = config_hash(expected["config"])
                expected_feature_hash = feature_config_hash(expected["config"])
                exact_match = (
                    manifest.get("data_fingerprint") == expected["data_fingerprint"]
                    and state["data_fingerprint"] == expected["data_fingerprint"]
                    and state["raw_entries"] == expected["raw_entries"]
                    and manifest.get("config_hash") == expected_config_hash
                    and manifest.get("feature_config_hash") == expected_feature_hash
                )
                if exact_match:
                    recovery.update(
                        {
                            "status": "resumed",
                            "resumed_cursor": cursor,
                            "resumed_global_version": manifest["global_version"],
                            "completed_batches": prefix_ids,
                            "next_batch": (
                                plan["batches"][cursor]["batch_id"]
                                if cursor < len(plan["batches"])
                                else None
                            ),
                        }
                    )
                    return state, recovery, events

                state, event = _reconcile_external_changes(
                    state=state,
                    expected=expected,
                    dependencies=dependencies,
                    store=store,
                    cursor=cursor,
                    crash_after_stage=crash_after_stage,
                )
                events.append(event)
                recovery.update(
                    {
                        "status": "external_reconcile",
                        "reason": event["reason"],
                        "resumed_cursor": cursor,
                        "resumed_global_version": state["manifest"]["global_version"],
                        "completed_batches": prefix_ids,
                        "next_batch": (
                            plan["batches"][cursor]["batch_id"]
                            if cursor < len(plan["batches"])
                            else None
                        ),
                    }
                )
                return state, recovery, events

    reset_reason = recovery.get("manifest_status")
    if loaded_snapshot.get("present"):
        reset_reason = reset_reason or "snapshot_metadata_mismatch"
    state, committed = _build_baseline(
        store=store,
        base_entries=base_entries,
        base_config=base_config,
        dependencies=dependencies,
        start_global_version=0,
    )
    event = {
        "event": "baseline",
        "invalidated_stages": list(LOGICAL_STAGES),
        "reused_stages": [],
        "recomputed_pair_counts": {
            "features": len(state["features"]),
            "scoring": len(state["scores"]),
            "discrimination": len(state["scores"]),
            "summary": 1,
        },
        "reused_pair_counts": {
            "features": 0,
            "scoring": 0,
            "discrimination": 0,
            "summary": 0,
        },
        "snapshot_version": committed["global_version"],
    }
    events.append(event)
    recovery.update(
        {
            "status": "initialized" if not loaded_snapshot.get("present") else "reset",
            "reason": reset_reason,
            "resumed_cursor": 0,
            "resumed_global_version": committed["global_version"],
            "completed_batches": [],
            "next_batch": plan["batches"][0]["batch_id"] if plan["batches"] else None,
        }
    )
    return state, recovery, events


def _build_baseline(
    *,
    store: SnapshotStore,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    dependencies: dict[str, Any],
    start_global_version: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    loaded = load_records(base_entries)
    feature_result = extract_many(loaded["valid"], base_config, "BASE")
    score_result = score_many(feature_result["entries"], base_config, "BASE")
    data_fp = data_fingerprint(base_entries, loaded)
    summary = compute_summary(
        loaded,
        feature_result["entries"],
        score_result["entries"],
        loaded["failures"],
        feature_result["failures"],
        score_result["failures"],
        base_config,
        ["BASE"],
        data_fp,
        dependencies,
    )
    state = {
        "config": copy.deepcopy(base_config),
        "raw_entries": copy.deepcopy(base_entries),
        "loaded": loaded,
        "data_fingerprint": data_fp,
        "features": feature_result["entries"],
        "feature_failures": feature_result["failures"],
        "bucket_references": feature_result["bucket_references"],
        "scores": score_result["entries"],
        "score_failures": score_result["failures"],
        "summary": summary,
        "stage_versions": {
            "features": start_global_version,
            "scores": start_global_version,
            "summary": start_global_version,
        },
    }
    committed = _commit_state(
        store,
        state,
        cursor=0,
        completed_batches=[],
        batch_hashes=[],
        global_version=start_global_version,
        feature_changed=True,
        score_changed=True,
        summary_changed=True,
        crash_after_stage=None,
    )
    return state, committed


def _reconcile_external_changes(
    *,
    state: dict[str, Any],
    expected: dict[str, Any],
    dependencies: dict[str, Any],
    store: SnapshotStore,
    cursor: int,
    crash_after_stage: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    old_loaded = state["loaded"]
    old_features = state["features"]
    old_scores = state["scores"]
    old_config = state["config"]
    new_config = copy.deepcopy(expected["config"])
    new_raw_entries = copy.deepcopy(expected["raw_entries"])
    new_loaded = expected["loaded"]
    new_ids = set(new_loaded["valid"])
    old_ids = set(old_loaded["valid"])
    added = sorted(new_ids.difference(old_ids))
    removed = sorted(old_ids.difference(new_ids))
    updated = sorted(
        pair_id
        for pair_id in old_ids.intersection(new_ids)
        if old_loaded["record_fingerprints"][pair_id]
        != new_loaded["record_fingerprints"][pair_id]
    )
    changed_data = sorted(set(added) | set(updated))
    feature_projection_changed = feature_projection(old_config) != feature_projection(
        new_config
    )
    scoring_changed = scoring_projection(old_config) != scoring_projection(new_config)
    raw_changed = new_raw_entries != state["raw_entries"]
    failures_changed = old_loaded["failures"] != new_loaded["failures"]

    if feature_projection_changed:
        affected_features = sorted(new_ids)
    else:
        affected_features = sorted(
            set(changed_data).union(pair_id for pair_id in new_ids if pair_id not in old_features)
        )

    old_changed_buckets = _bucket_refs_for(
        old_features, set(affected_features) | set(removed)
    )
    features = {
        pair_id: old_features[pair_id]
        for pair_id in sorted(old_features)
        if pair_id in new_ids
        and pair_id not in set(affected_features)
    }
    extract_result = extract_many(
        {pair_id: new_loaded["valid"][pair_id] for pair_id in affected_features},
        new_config,
        "EXTERNAL",
    )
    features.update(extract_result["entries"])
    feature_failures = extract_result["failures"]
    bucket_references: dict[str, list[str]] = {}
    for pair_id in sorted(features):
        for bucket in features[pair_id]["referenced_buckets"]:
            bucket_references.setdefault(str(bucket), []).append(pair_id)
    for bucket in bucket_references:
        bucket_references[bucket].sort()
    bucket_references = dict(
        sorted(bucket_references.items(), key=lambda item: int(item[0]))
    )

    if scoring_changed or feature_projection_changed:
        score_recompute = sorted(pair_id for pair_id in features if pair_id in new_ids)
    else:
        score_recompute = sorted(
            set(changed_data).union(
                pair_id for pair_id in features if pair_id not in old_scores
            )
        )
    scores = {
        pair_id: old_scores[pair_id]
        for pair_id in sorted(old_scores)
        if pair_id in new_ids
        and pair_id not in set(score_recompute)
        and pair_id in features
    }
    score_result = score_many(
        {pair_id: features[pair_id] for pair_id in score_recompute},
        new_config,
        "EXTERNAL",
    )
    scores.update(score_result["entries"])
    score_failures = score_result["failures"]

    new_active_buckets = sorted(
        {
            int(bucket)
            for feature in features.values()
            for bucket in feature["referenced_buckets"]
        }
    )
    new_changed_buckets = _bucket_refs_for(features, set(affected_features))
    changed_buckets = sorted(old_changed_buckets | new_changed_buckets)
    old_active_buckets = {
        int(bucket)
        for feature in old_features.values()
        for bucket in feature["referenced_buckets"]
    }
    orphaned_buckets = sorted(old_active_buckets.difference(new_active_buckets))

    feature_invalidated = (
        raw_changed
        or failures_changed
        or feature_projection_changed
        or bool(affected_features)
        or bool(removed)
    )
    score_invalidated = (
        scoring_changed
        or feature_invalidated
        or bool(score_recompute)
        or bool(removed)
    )
    invalidated_stages: list[str] = []
    if feature_invalidated:
        invalidated_stages.append("features")
    if score_invalidated:
        invalidated_stages.extend(["scoring", "discrimination"])
    if invalidated_stages:
        invalidated_stages.append("summary")
    reused_stages = [
        stage for stage in LOGICAL_STAGES if stage not in invalidated_stages
    ]

    data_fp = expected["data_fingerprint"]
    batch_ids = ["BASE"] + list(state["manifest"]["completed_batches"])
    if invalidated_stages:
        summary = compute_summary(
            new_loaded,
            features,
            scores,
            new_loaded["failures"],
            feature_failures,
            score_failures,
            new_config,
            batch_ids,
            data_fp,
            dependencies,
        )
    else:
        summary = state["summary"]
    affected_summaries = affected_summary_outputs(
        dependencies,
        changed_batches={"EXTERNAL"},
        changed_buckets=set(changed_buckets),
        changed_stages=set(invalidated_stages),
    )
    new_state = {
        "config": new_config,
        "raw_entries": new_raw_entries,
        "loaded": new_loaded,
        "data_fingerprint": data_fp,
        "features": features,
        "feature_failures": feature_failures,
        "bucket_references": bucket_references,
        "scores": scores,
        "score_failures": score_failures,
        "summary": summary,
        "stage_versions": dict(state["stage_versions"]),
    }
    global_version = int(state["manifest"]["global_version"]) + 1
    committed = _commit_state(
        store,
        new_state,
        cursor=cursor,
        completed_batches=list(state["manifest"]["completed_batches"]),
        batch_hashes=list(state["manifest"]["batch_hashes"]),
        global_version=global_version,
        feature_changed=feature_invalidated,
        score_changed=score_invalidated,
        summary_changed=bool(invalidated_stages),
        crash_after_stage=crash_after_stage,
    )
    reasons: list[str] = []
    if feature_projection_changed:
        reasons.append("feature_projection_changed")
    if changed_data or removed:
        reasons.append("external_data_changed")
    if scoring_changed:
        reasons.append("scoring_or_threshold_changed")
    if raw_changed and not reasons:
        reasons.append("input_representation_changed")
    if not reasons:
        reasons.append("snapshot_metadata_refreshed")
    feature_reused = sorted(
        pair_id
        for pair_id in new_ids
        if pair_id in features and pair_id not in set(affected_features)
    )
    score_reused = sorted(
        pair_id for pair_id in scores if pair_id not in set(score_recompute)
    )
    event = {
        "event": "external_reconcile",
        "batch_id": None,
        "reason": "+".join(reasons),
        "data_changes": {
            "added_pairs": added,
            "removed_pairs": removed,
            "updated_pairs": updated,
            "load_failures_changed": failures_changed,
        },
        "config_changes": {
            "feature_projection_changed": feature_projection_changed,
            "scoring_changed": scoring_changed,
        },
        "invalidated_stages": invalidated_stages,
        "reused_stages": reused_stages,
        "affected_pairs": sorted(set(changed_data) | set(removed)),
        "affected_buckets": changed_buckets,
        "orphaned_buckets": orphaned_buckets,
        "summary_outputs_affected": affected_summaries,
        "recomputed_pair_counts": {
            "features": len(affected_features),
            "scoring": len(score_recompute),
            "discrimination": len(score_recompute),
            "summary": 1 if invalidated_stages else 0,
        },
        "reused_pair_counts": {
            "features": len(feature_reused),
            "scoring": len(score_reused),
            "discrimination": len(score_reused),
            "summary": 0 if invalidated_stages else 1,
        },
        "feature_reused_pair_ids": feature_reused,
        "feature_recomputed_pair_ids": affected_features,
        "score_reused_pair_ids": score_reused,
        "score_recomputed_pair_ids": score_recompute,
        "snapshot_version": committed["global_version"],
        "stage_versions": committed["stage_versions"],
        "snapshot_commit_interval": new_config["snapshot_commit_interval"],
        "invariant_checks": {
            "features_reused_only_for_unchanged": set(feature_reused).isdisjoint(
                set(affected_features)
            ),
            "scoring_reused_only_when_config_stable": not scoring_changed
            or not score_reused,
            "summary_not_reused_when_upstream_invalid": not invalidated_stages,
        },
    }
    event["invariant_checks"]["all_pass"] = all(
        event["invariant_checks"].values()
    )
    return new_state, event


def _commit_state(
    store: SnapshotStore,
    state: dict[str, Any],
    *,
    cursor: int,
    completed_batches: list[str],
    batch_hashes: list[str],
    global_version: int,
    feature_changed: bool,
    score_changed: bool,
    summary_changed: bool,
    crash_after_stage: str | None,
) -> dict[str, Any]:
    previous_versions = dict(state["stage_versions"])
    if feature_changed:
        previous_versions["features"] = global_version
    if score_changed:
        previous_versions["scores"] = global_version
    if summary_changed:
        previous_versions["summary"] = global_version
    state["stage_versions"] = previous_versions
    payloads = _build_payloads(
        state,
        previous_versions["features"],
        previous_versions["scores"],
        previous_versions["summary"],
    )
    manifest = store.commit(
        global_version=global_version,
        cursor=cursor,
        completed_batches=completed_batches,
        batch_hashes=batch_hashes,
        config_hash=config_hash(state["config"]),
        feature_config_hash=feature_config_hash(state["config"]),
        data_fingerprint=state["data_fingerprint"],
        effective_config=state["config"],
        stages=payloads,
        stage_versions=previous_versions,
        commit_interval=state["config"]["snapshot_commit_interval"],
        crash_after_stage=crash_after_stage,
    )
    state["manifest"] = manifest
    return manifest


def _apply_batch(
    *,
    state: dict[str, Any],
    batch: dict[str, Any],
    dependencies: dict[str, Any],
    global_version: int,
    completed_batches: list[str],
    batch_hashes: list[str],
    fault_map: dict[str, str] | None,
    crash_after_stage: str | None,
    store: SnapshotStore,
    batch_delay_seconds: float = 0.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .config import apply_config_updates
    from .plan import batch_hash

    old_loaded = state["loaded"]
    old_features = state["features"]
    old_scores = state["scores"]
    old_config = state["config"]
    new_raw_entries = apply_batch_to_raw(state["raw_entries"], batch)
    new_loaded = load_records(new_raw_entries)
    new_config = apply_config_updates(old_config, batch)

    old_record_ids = set(old_loaded["valid"])
    new_record_ids = set(new_loaded["valid"])
    added_pairs = sorted(new_record_ids.difference(old_record_ids))
    removed_pairs = sorted(old_record_ids.difference(new_record_ids))
    common_pairs = old_record_ids.intersection(new_record_ids)
    updated_pairs = sorted(
        pair_id
        for pair_id in common_pairs
        if old_loaded["record_fingerprints"][pair_id]
        != new_loaded["record_fingerprints"][pair_id]
    )
    changed_data_pairs = sorted(set(added_pairs) | set(updated_pairs))
    load_failures_changed = old_loaded["failures"] != new_loaded["failures"]
    raw_changed = new_raw_entries != state["raw_entries"]
    data_stage_changed = raw_changed or load_failures_changed

    scoring_changed = scoring_projection(old_config) != scoring_projection(new_config)
    feature_projection_changed = feature_projection(old_config) != feature_projection(
        new_config
    )

    if feature_projection_changed:
        affected_feature_pairs = sorted(new_record_ids)
    else:
        affected_feature_pairs = changed_data_pairs
    removed_feature_pairs = removed_pairs

    old_changed_buckets = _bucket_refs_for(
        old_features, set(affected_feature_pairs) | set(removed_feature_pairs)
    )
    features = {
        pair_id: old_features[pair_id]
        for pair_id in sorted(old_features)
        if pair_id not in set(removed_feature_pairs)
        and pair_id not in set(affected_feature_pairs)
    }
    feature_failures = [
        failure
        for failure in state["feature_failures"]
        if failure.get("pair_id") not in set(removed_feature_pairs)
        and failure.get("pair_id") not in set(affected_feature_pairs)
    ]
    pairs_to_extract = {
        pair_id: new_loaded["valid"][pair_id] for pair_id in affected_feature_pairs
    }
    extract_result = extract_many(
        pairs_to_extract, new_config, batch["batch_id"], fault_map=fault_map
    )
    features.update(extract_result["entries"])
    feature_failures.extend(extract_result["failures"])

    expected_projection = feature_projection(new_config)
    retry_cache_pairs: list[str] = []
    for pair_id in sorted(new_record_ids):
        if pair_id in features:
            expected_key = sha256_obj(
                {
                    "content_fingerprint": features[pair_id]["content_fingerprint"],
                    "feature_projection": expected_projection,
                }
            )
            if features[pair_id]["cache_key"] != expected_key:
                retry_cache_pairs.append(pair_id)
    if retry_cache_pairs:
        retry_result = extract_many(
            {
                pair_id: new_loaded["valid"][pair_id]
                for pair_id in retry_cache_pairs
            },
            new_config,
            batch["batch_id"],
            fault_map=fault_map,
        )
        features.update(retry_result["entries"])
        feature_failures.extend(retry_result["failures"])
        affected_feature_pairs = sorted(
            set(affected_feature_pairs) | set(retry_cache_pairs)
        )

    bucket_references: dict[str, list[str]] = {}
    for pair_id in sorted(features):
        for bucket in features[pair_id]["referenced_buckets"]:
            bucket_references.setdefault(str(bucket), []).append(pair_id)
    for bucket in bucket_references:
        bucket_references[bucket].sort()
    bucket_references = dict(
        sorted(bucket_references.items(), key=lambda item: int(item[0]))
    )

    if scoring_changed:
        score_recompute_pairs = sorted(
            pair_id for pair_id in features if pair_id in new_loaded["valid"]
        )
    else:
        score_recompute_pairs = sorted(
            pair_id
            for pair_id in set(changed_data_pairs).union(retry_cache_pairs)
            if pair_id in features
        )
    scores = {
        pair_id: old_scores[pair_id]
        for pair_id in sorted(old_scores)
        if pair_id not in set(removed_pairs)
        and pair_id not in set(score_recompute_pairs)
    }
    score_failures = [
        failure
        for failure in state["score_failures"]
        if failure.get("pair_id") not in set(removed_pairs)
        and failure.get("pair_id") not in set(score_recompute_pairs)
    ]
    score_result = score_many(
        {pair_id: features[pair_id] for pair_id in score_recompute_pairs},
        new_config,
        batch["batch_id"],
        fault_map=fault_map,
    )
    scores.update(score_result["entries"])
    score_failures.extend(score_result["failures"])

    new_active_buckets = sorted(
        {
            int(bucket)
            for feature in features.values()
            for bucket in feature["referenced_buckets"]
        }
    )
    new_changed_buckets = _bucket_refs_for(
        features, set(affected_feature_pairs)
    )
    changed_buckets = sorted(old_changed_buckets | new_changed_buckets)
    old_active_buckets = {
        int(bucket)
        for feature in old_features.values()
        for bucket in feature["referenced_buckets"]
    }
    orphaned_buckets = sorted(old_active_buckets.difference(new_active_buckets))

    feature_stage_invalid = (
        data_stage_changed or bool(affected_feature_pairs) or bool(removed_pairs)
    )
    score_stage_invalid = (
        scoring_changed
        or feature_stage_invalid
        or bool(score_recompute_pairs)
    )
    invalidated_stages: list[str] = []
    if feature_stage_invalid:
        invalidated_stages.append("features")
    if score_stage_invalid:
        invalidated_stages.extend(["scoring", "discrimination"])
    if invalidated_stages:
        invalidated_stages.append("summary")
    reused_stages = [
        stage for stage in LOGICAL_STAGES if stage not in invalidated_stages
    ]

    if invalidated_stages:
        batch_ids = ["BASE"] + completed_batches + [batch["batch_id"]]
        new_data_fp = data_fingerprint(new_raw_entries, new_loaded)
        summary = compute_summary(
            new_loaded,
            features,
            scores,
            new_loaded["failures"],
            feature_failures,
            score_failures,
            new_config,
            batch_ids,
            new_data_fp,
            dependencies,
        )
    else:
        new_data_fp = state["data_fingerprint"]
        summary = state["summary"]

    changed_stages = set(invalidated_stages)
    if scoring_changed:
        changed_stages.update({"weights", "threshold"})
    affected_summaries = affected_summary_outputs(
        dependencies,
        changed_batches={batch["batch_id"]},
        changed_buckets=set(changed_buckets),
        changed_stages=changed_stages,
    )

    new_state = {
        "config": new_config,
        "raw_entries": new_raw_entries,
        "loaded": new_loaded,
        "data_fingerprint": new_data_fp,
        "features": features,
        "feature_failures": feature_failures,
        "bucket_references": bucket_references,
        "scores": scores,
        "score_failures": score_failures,
        "summary": summary,
        "stage_versions": dict(state["stage_versions"]),
    }
    feature_reused_pairs = sorted(
        pair_id
        for pair_id in new_record_ids
        if pair_id in features and pair_id not in set(affected_feature_pairs)
    )
    score_reused_pairs = sorted(
        pair_id
        for pair_id in scores
        if pair_id not in set(score_recompute_pairs)
    )
    invariant_checks = {
        "features_not_reused_when_data_changed_for_pair": set(
            feature_reused_pairs
        ).isdisjoint(set(affected_feature_pairs)),
        "scoring_not_reused_when_config_changed": (
            not scoring_changed or not score_reused_pairs
        ),
        "summary_not_reused_when_upstream_invalid": (
            not invalidated_stages or summary is not state["summary"]
        ),
    }
    invariant_checks["all_pass"] = all(invariant_checks.values())

    if batch_delay_seconds > 0:
        time.sleep(batch_delay_seconds)

    next_completed_batches = completed_batches + [batch["batch_id"]]
    next_batch_hashes = batch_hashes + [batch_hash(batch)]
    committed = _commit_state(
        store,
        new_state,
        cursor=len(next_completed_batches),
        completed_batches=next_completed_batches,
        batch_hashes=next_batch_hashes,
        global_version=global_version,
        feature_changed=feature_stage_invalid,
        score_changed=score_stage_invalid,
        summary_changed=bool(invalidated_stages),
        crash_after_stage=crash_after_stage,
    )
    event = {
        "event": "batch",
        "batch_id": batch["batch_id"],
        "cursor_before": len(completed_batches),
        "cursor_after": len(next_completed_batches),
        "global_version_before": global_version - 1,
        "global_version_after": committed["global_version"],
        "data_changes": {
            "added_pairs": added_pairs,
            "removed_pairs": removed_pairs,
            "updated_pairs": updated_pairs,
            "load_failures_changed": load_failures_changed,
        },
        "config_changes": {
            "scoring_changed": scoring_changed,
            "feature_projection_changed": feature_projection_changed,
            "weights_changed": old_config["weights"] != new_config["weights"],
            "threshold_changed": old_config["reward_delta_threshold"]
            != new_config["reward_delta_threshold"],
            "score_precision_changed": old_config["score_precision"]
            != new_config["score_precision"],
        },
        "invalidated_stages": invalidated_stages,
        "reused_stages": reused_stages,
        "affected_pairs": sorted(set(changed_data_pairs) | set(removed_pairs)),
        "affected_buckets": changed_buckets,
        "orphaned_buckets": orphaned_buckets,
        "summary_outputs_affected": affected_summaries,
        "recomputed_pair_counts": {
            "features": len(affected_feature_pairs),
            "scoring": len(score_recompute_pairs),
            "discrimination": len(score_recompute_pairs),
            "summary": 1 if invalidated_stages else 0,
        },
        "reused_pair_counts": {
            "features": len(feature_reused_pairs),
            "scoring": len(score_reused_pairs),
            "discrimination": len(score_reused_pairs),
            "summary": 0 if invalidated_stages else 1,
        },
        "feature_reused_pair_ids": feature_reused_pairs,
        "feature_recomputed_pair_ids": affected_feature_pairs,
        "score_reused_pair_ids": score_reused_pairs,
        "score_recomputed_pair_ids": score_recompute_pairs,
        "snapshot_version": committed["global_version"],
        "stage_versions": committed["stage_versions"],
        "snapshot_commit_interval": new_config["snapshot_commit_interval"],
        "invariant_checks": invariant_checks,
    }
    return new_state, event


def run_incremental(
    *,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
    dependencies: dict[str, Any],
    state_dir: str,
    stop_event: Any | None = None,
    fault_map: dict[str, str] | None = None,
    crash_after_stage: str | None = None,
    batch_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    store = SnapshotStore(state_dir)
    state, recovery, events = _resume_or_initialize(
        store=store,
        base_entries=base_entries,
        base_config=base_config,
        plan=plan,
        dependencies=dependencies,
        crash_after_stage=crash_after_stage,
    )
    started_cursor = int(state["manifest"]["cursor"])
    stopped = False
    for index in range(started_cursor, len(plan["batches"])):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            break
        batch = plan["batches"][index]
        global_version = int(state["manifest"]["global_version"]) + 1
        state, event = _apply_batch(
            state=state,
            batch=batch,
            dependencies=dependencies,
            global_version=global_version,
            completed_batches=list(state["manifest"]["completed_batches"]),
            batch_hashes=list(state["manifest"]["batch_hashes"]),
            fault_map=fault_map,
            crash_after_stage=crash_after_stage,
            store=store,
            batch_delay_seconds=batch_delay_seconds,
        )
        events.append(event)
        if stop_event is not None and stop_event.is_set():
            stopped = True
            break

    completed_batches = list(state["manifest"]["completed_batches"])
    canonical = _canonical_result(state, ["BASE"] + completed_batches)
    verified_snapshot = store.load()
    return {
        "state": state,
        "events": events,
        "recovery": recovery,
        "stopped_early": stopped,
        "canonical_result": canonical,
        "manifest": state["manifest"],
        "snapshot_verification": {
            "valid": bool(verified_snapshot.get("valid")),
            "reason": verified_snapshot.get("reason"),
            "orphan_versions": verified_snapshot.get("orphan_versions", []),
        },
    }


def build_full_result(
    *,
    base_entries: list[dict[str, Any]],
    base_config: dict[str, Any],
    plan: dict[str, Any],
    dependencies: dict[str, Any],
    concurrency_override: int | None = None,
) -> dict[str, Any]:
    materialized = materialize_final(base_entries, base_config, plan)
    config = copy.deepcopy(materialized["config"])
    if concurrency_override is not None:
        config["concurrency"] = int(concurrency_override)
    loaded = materialized["loaded"]
    feature_result = extract_many(loaded["valid"], config, "FULL")
    score_result = score_many(feature_result["entries"], config, "FULL")
    data_fp = materialized["data_fingerprint"]
    batch_ids = ["BASE"] + materialized["batch_ids"]
    summary = compute_summary(
        loaded,
        feature_result["entries"],
        score_result["entries"],
        loaded["failures"],
        feature_result["failures"],
        score_result["failures"],
        config,
        batch_ids,
        data_fp,
        dependencies,
    )
    state = {
        "config": config,
        "raw_entries": materialized["raw_entries"],
        "loaded": loaded,
        "data_fingerprint": data_fp,
        "features": feature_result["entries"],
        "feature_failures": feature_result["failures"],
        "bucket_references": feature_result["bucket_references"],
        "scores": score_result["entries"],
        "score_failures": score_result["failures"],
        "summary": summary,
    }
    return _canonical_result(state, batch_ids)


def _differences(left: Any, right: Any, path: str = "$", limit: int = 200) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(a: Any, b: Any, current: str) -> None:
        if len(result) >= limit:
            return
        if type(a) is not type(b):
            result.append({"path": current, "left_type": type(a).__name__, "right_type": type(b).__name__, "left": a, "right": b})
            return
        if isinstance(a, dict):
            for key in sorted(set(a).union(b)):
                if key not in a:
                    result.append({"path": f"{current}.{key}", "left": "<missing>", "right": b[key]})
                elif key not in b:
                    result.append({"path": f"{current}.{key}", "left": a[key], "right": "<missing>"})
                else:
                    walk(a[key], b[key], f"{current}.{key}")
                if len(result) >= limit:
                    return
        elif isinstance(a, list):
            if len(a) != len(b):
                result.append({"path": f"{current}.length", "left": len(a), "right": len(b)})
            for index in range(min(len(a), len(b))):
                walk(a[index], b[index], f"{current}[{index}]")
                if len(result) >= limit:
                    return
        elif a != b:
            result.append({"path": current, "left": a, "right": b})

    walk(left, right, path)
    return result


def compare_results(incremental: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    differences = _differences(incremental, full)
    checked_fields = [
        "feature_entries",
        "score_entries",
        "reward_delta",
        "boundary_decisions",
        "summary",
        "failure_list",
        "data_fingerprint",
        "config_hash",
        "bucket_references",
    ]
    return {
        "identical": not differences,
        "checked_fields": checked_fields,
        "difference_count": len(differences),
        "differences": differences,
    }
