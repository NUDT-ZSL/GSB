"""End-to-end deterministic preference-data pipeline with incremental caching."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .cache import ArtifactCache, pair_quality_key, stage_cache_key
from .canonical import (
    atomic_write_json,
    content_hash,
    file_sha256,
    read_json,
    sealed,
)
from .config import ConfigSnapshot, load_config
from .consistency import compute_consistency
from .dataset import input_digest, load_pairs, pair_digests
from .errors import (
    E_SELFCHECK_FAILED,
    E_WORKER_FAILURE,
    W_DEGRADED_GROUPING,
    BusinessError,
)
from .neardup import compute_neardup, normalize_text
from .quality import quality_worker
from .sampling import compute_sampling
from .scheduler import build_schedule
from .selfcheck import run_selfcheck
from .snapshots import (
    SnapshotStore,
    check_run_manifest_guard,
    publish_run_manifest,
)


def _cache_key_extra(
    *,
    active_pair_ids: list[str],
    schedule_report: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = {
        "active_pair_ids": active_pair_ids,
        "plan_fingerprint": schedule_report["plan_fingerprint"],
    }
    if extra:
        value.update(extra)
    return value


def _load_previous_state(cache_root: Path) -> dict[str, Any] | None:
    path = cache_root / "state.json"
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def _write_state(cache_root: Path, state: dict[str, Any]) -> None:
    atomic_write_json(cache_root / "state.json", state, canonical=True)


def _stage_from_cache(
    cache: ArtifactCache,
    stage: str,
    key: str,
) -> tuple[dict[str, Any] | None, bool]:
    body = cache.get(stage, key)
    if body is None:
        return None, False
    return sealed(body), True


def _execute_quality_misses(
    misses_by_batch: dict[int, list[dict[str, Any]]],
    *,
    workers: int,
    quality_config: dict[str, Any],
    decimals: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not misses_by_batch:
        return [], []
    payloads = [
        {
            "batch_id": batch_id,
            "pairs": misses_by_batch[batch_id],
            "quality_config": quality_config,
            "decimals": decimals,
        }
        for batch_id in sorted(misses_by_batch)
    ]
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(quality_worker, payload) for payload in payloads]
        for payload, future in zip(payloads, futures):
            try:
                result = future.result()
                records.extend(result["records"])
                errors.extend(result["errors"])
            except Exception as exc:  # noqa: BLE001 - attribute a dead batch
                for pair in payload["pairs"]:
                    errors.append(
                        {
                            "code": E_WORKER_FAILURE,
                            "stage": "quality",
                            "pair_ids": [str(pair["pair_id"])],
                            "batch_id": int(payload["batch_id"]),
                            "message": str(exc),
                            "exception_type": type(exc).__name__,
                        }
                    )
    return records, errors


def _partition_schedule_batches(
    schedule_report: dict[str, Any],
    missing_ids: set[str],
) -> dict[int, list[dict[str, Any]]]:
    batches: dict[int, list[dict[str, Any]]] = {}
    for batch in schedule_report["batches"]:
        selected = [
            pair for pair in batch["_pairs"] if str(pair["pair_id"]) in missing_ids
        ]
        if selected:
            batches[int(batch["batch_id"])] = selected
    return batches


def _quality_rank_records(records: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        records.values(),
        key=lambda record: (-float(record["pair_score"]), str(record["pair_id"])),
    )
    output: list[dict[str, Any]] = []
    for rank, record in enumerate(ranked):
        item = dict(record)
        item["quality_rank"] = rank
        output.append(item)
    return output


def _sorted_stage_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    return sealed(payload)


def _previous_cluster_members(
    previous: dict[str, Any] | None,
    pair_id: str,
) -> set[str]:
    if not previous:
        return set()
    cluster_id = previous.get("cluster_by_pair", {}).get(pair_id)
    if cluster_id is None:
        return set()
    return set(previous.get("cluster_members", {}).get(cluster_id, []))


def _current_cluster_members(
    neardup_artifact: dict[str, Any],
    pair_id: str,
) -> set[str]:
    cluster_id = neardup_artifact["assignments"].get(pair_id, {}).get("cluster_id")
    if cluster_id is None:
        return set()
    for cluster in neardup_artifact["clusters"]:
        if cluster["cluster_id"] == cluster_id:
            return set(cluster["members"])
    return set()


def _compute_stage_audit(
    *,
    previous: dict[str, Any] | None,
    snapshot: ConfigSnapshot,
    input_pair_digests: dict[str, str],
    all_pairs: list[dict[str, Any]],
    active_pair_ids: list[str],
    schedule_report: dict[str, Any],
    qualidade_hits: set[str],
    qualidade_misses: set[str],
    neardup_hit: bool,
    consistency_hit: bool,
    sampling_hit: bool,
    manifest_hit: bool,
    neardup_artifact: dict[str, Any],
    seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    previous = previous or {}
    previous_pair_digests = previous.get("pair_input_digests", {})
    changed_input = {
        pair_id
        for pair_id in set(previous_pair_digests) | set(input_pair_digests)
        if previous_pair_digests.get(pair_id) != input_pair_digests.get(pair_id)
    }
    current_admission = schedule_report["pair_paths"]
    previous_admission = previous.get("pair_admission", {})
    admission_changes = {
        pair_id
        for pair_id in set(current_admission) | set(previous_admission)
        if current_admission.get(pair_id, {}).get("status")
        != previous_admission.get(pair_id, {}).get("status")
    }
    previous_versions = previous.get("stage_config_versions", {})
    current_versions = {
        stage: snapshot.stage_version(stage)
        for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
    }
    quality_cfg_changed = previous_versions.get("quality") != current_versions["quality"]
    neardup_cfg_changed = previous_versions.get("neardup") != current_versions["neardup"]
    consistency_cfg_changed = (
        previous_versions.get("consistency") != current_versions["consistency"]
    )
    sampling_cfg_changed = previous_versions.get("sampling") != current_versions["sampling"]
    previous_seed = previous.get("sampling_seed")
    seed_changed = previous_seed is not None and previous_seed != seed

    active = set(active_pair_ids)
    quality_recomputed = set(qualidade_misses) & active
    quality_reused = set(qualidade_hits) & active
    if not previous or quality_cfg_changed:
        quality_affected = set(active)
    else:
        quality_affected = (
            changed_input | admission_changes | quality_recomputed
        ) & active

    if not previous or neardup_cfg_changed:
        neardup_affected = set(active)
    else:
        base = quality_affected | (changed_input & active)
        expanded = set(base)
        for pair_id in base:
            expanded |= _previous_cluster_members(previous, pair_id)
            expanded |= _current_cluster_members(neardup_artifact, pair_id)
        neardup_affected = expanded & active

    if not previous or consistency_cfg_changed:
        consistency_affected = set(active)
    elif neardup_affected:
        prompt_keys: dict[str, str] = {}
        for pair in all_pairs:
            pair_id = str(pair["pair_id"])
            prompt_keys[pair_id] = content_hash(normalize_text(str(pair["prompt"])))
        affected_prompts = {prompt_keys[pair_id] for pair_id in neardup_affected}
        consistency_affected = {
            pair_id for pair_id in active if prompt_keys[pair_id] in affected_prompts
        }
    else:
        consistency_affected = set()

    if not previous or sampling_cfg_changed or seed_changed:
        sampling_affected = set(active)
    elif consistency_affected or neardup_affected:
        sampling_affected = set(active)
    else:
        sampling_affected = set()

    manifest_affected = (
        set(str(pair["pair_id"]) for pair in all_pairs)
        if changed_input
        or quality_affected
        or neardup_affected
        or consistency_affected
        or sampling_affected
        or any(
            previous_versions.get(stage) != current_versions[stage]
            for stage in current_versions
        )
        else set()
    )

    def status(hit: bool, recomputed_count: int) -> str:
        return "reused" if hit and recomputed_count == 0 else "recomputed"

    audit: dict[str, Any] = {
        "quality": {
            "status": (
                "reused"
                if quality_reused and not quality_recomputed
                else "recomputed"
                if not quality_reused
                else "partially_recomputed"
            ),
            "recomputed_pair_ids": sorted(quality_recomputed),
            "reused_pair_ids": sorted(quality_reused),
            "affected_pair_ids": sorted(quality_affected),
        },
        "neardup": {
            "status": status(neardup_hit, 0 if neardup_hit else len(active)),
            "recomputed_pair_ids": [] if neardup_hit else sorted(active),
            "reused_pair_ids": sorted(active) if neardup_hit else [],
            "affected_pair_ids": sorted(neardup_affected),
        },
        "consistency": {
            "status": status(consistency_hit, 0 if consistency_hit else len(active)),
            "recomputed_pair_ids": [] if consistency_hit else sorted(active),
            "reused_pair_ids": sorted(active) if consistency_hit else [],
            "affected_pair_ids": sorted(consistency_affected),
        },
        "sampling": {
            "status": status(sampling_hit, 0 if sampling_hit else len(active)),
            "recomputed_pair_ids": [] if sampling_hit else sorted(active),
            "reused_pair_ids": sorted(active) if sampling_hit else [],
            "affected_pair_ids": sorted(sampling_affected),
        },
        "manifest": {
            "status": "reused" if manifest_hit else "recomputed",
            "recomputed_pair_ids": [] if manifest_hit else sorted(
                str(pair["pair_id"]) for pair in all_pairs
            ),
            "reused_pair_ids": sorted(str(pair["pair_id"]) for pair in all_pairs)
            if manifest_hit
            else [],
            "affected_pair_ids": sorted(manifest_affected),
        },
    }
    incremental = {
        "has_previous_state": bool(previous),
        "changed_input_pair_ids": sorted(changed_input),
        "admission_changed_pair_ids": sorted(admission_changes),
        "stage_config_changes": {
            stage: previous_versions.get(stage) != current_versions[stage]
            for stage in current_versions
        },
        "recomputed_stages": sorted(
            stage for stage, item in audit.items() if item["status"] != "reused"
        ),
        "reused_stages": sorted(
            stage for stage, item in audit.items() if item["status"] == "reused"
        ),
        "affected_pair_ids_by_stage": {
            stage: item["affected_pair_ids"] for stage, item in audit.items()
        },
    }
    return audit, incremental


def run_pipeline(
    *,
    input_path: str | Path,
    config_path: str | Path,
    out_dir: str | Path,
    cache_root: str | Path,
    cold_cache: bool = False,
) -> dict[str, Any]:
    snapshot = load_config(config_path)
    cache_root_path = Path(cache_root)
    cache_root_path.mkdir(parents=True, exist_ok=True)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    manifest_path = out_path / "manifest.json"
    check_run_manifest_guard(manifest_path, snapshot)

    snapshot_store = SnapshotStore(cache_root_path / "snapshots")
    snapshot_store.publish(snapshot)

    pairs = load_pairs(input_path)
    pair_digest_map = pair_digests(pairs)
    dataset_digest = input_digest(pairs)
    pair_by_id = {str(pair["pair_id"]): pair for pair in pairs}
    previous_state = _load_previous_state(cache_root_path)
    cache = ArtifactCache(cache_root_path / "artifacts", read_enabled=not cold_cache)

    schedule_report = build_schedule(
        pairs,
        snapshot.payload["scheduler"],
        snapshot.payload["neardup"],
        snapshot.payload["priority_order"],
    )
    for batch in schedule_report["batches"]:
        batch["_pairs"] = [pair_by_id[pair_id] for pair_id in batch["pair_ids"]]

    admitted_ids = list(schedule_report["selected_plan"]["admitted_pair_ids"])
    quality_records_by_id: dict[str, dict[str, Any]] = {}
    quality_hits: set[str] = set()
    quality_misses: set[str] = set()
    quality_keys: dict[str, str] = {}
    missing_pairs: list[dict[str, Any]] = []
    for pair_id in admitted_ids:
        key = pair_quality_key(
            pair_digest=pair_digest_map[pair_id],
            quality_config_version=snapshot.stage_version("quality"),
            seed=int(snapshot.payload["seed"]),
            precision=str(snapshot.payload["precision"]),
        )
        quality_keys[pair_id] = key
        body = cache.get("quality", key)
        if body is None:
            quality_misses.add(pair_id)
            missing_pairs.append(pair_by_id[pair_id])
        else:
            record = body.get("record")
            if not isinstance(record, dict) or str(record.get("pair_id")) != pair_id:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    "cached quality record does not match pair",
                    pair_ids=[pair_id],
                )
            quality_records_by_id[pair_id] = dict(record)
            quality_hits.add(pair_id)

    missing_set = {str(pair["pair_id"]) for pair in missing_pairs}
    misses_by_batch = _partition_schedule_batches(schedule_report, missing_set)
    computed_records, worker_errors = _execute_quality_misses(
        misses_by_batch,
        workers=int(schedule_report["selected_plan"]["workers"]),
        quality_config=snapshot.payload["quality"],
        decimals=_precision_decimals(snapshot.payload["precision"]),
    )
    for record in computed_records:
        pair_id = str(record["pair_id"])
        quality_records_by_id[pair_id] = record
        cache.put("quality", quality_keys[pair_id], {"record": record})

    errors_by_pair: dict[str, dict[str, Any]] = {}
    for error in worker_errors:
        for pair_id in error.get("pair_ids", []):
            errors_by_pair[str(pair_id)] = error
    active_pair_ids = sorted(
        pair_id
        for pair_id in admitted_ids
        if pair_id not in errors_by_pair and pair_id in quality_records_by_id
    )
    active_set = set(active_pair_ids)
    active_pairs = [pair_by_id[pair_id] for pair_id in active_pair_ids]
    quality_recomputed = quality_misses & active_set
    cache.partial_quality = {
        "hit": len(quality_hits),
        "miss": len(quality_misses),
        "active": len(active_set),
    }

    quality_artifact = _sorted_stage_artifact(
        {
            "stage": "quality",
            "schema_version": "quality-stage/1.0",
            "input_digest": dataset_digest,
            "config_version": snapshot.stage_version("quality"),
            "sampling_seed": int(snapshot.payload["seed"]),
            "precision": snapshot.payload["precision"],
            "records": _quality_rank_records(quality_records_by_id),
            "admitted_pair_ids": sorted(admitted_ids),
            "successful_pair_ids": active_pair_ids,
            "worker_errors": sorted(
                worker_errors,
                key=lambda item: (
                    int(item.get("batch_id", -1)),
                    tuple(item.get("pair_ids", [])),
                ),
            ),
        }
    )

    neardup_input_extra = _cache_key_extra(
        active_pair_ids=active_pair_ids,
        schedule_report=schedule_report,
        extra={
            "candidate_mode": schedule_report["selected_plan"]["candidate_mode"],
            "comparison_limit": schedule_report["selected_plan"]["comparison_limit"],
        },
    )
    neardup_key = stage_cache_key(
        stage="neardup",
        input_digest=dataset_digest,
        upstream_artifact_hash=quality_artifact["content_hash"],
        config_version=snapshot.stage_version("neardup"),
        seed=int(snapshot.payload["seed"]),
        extra=neardup_input_extra,
        schema_version="neardup-cache/1.0",
    )
    neardup_artifact, neardup_hit = _stage_from_cache(cache, "neardup", neardup_key)
    if neardup_artifact is None:
        neardup_result = compute_neardup(
            active_pairs,
            quality_records_by_id,
            snapshot.payload["neardup"],
            candidate_mode=schedule_report["selected_plan"]["candidate_mode"],
            comparison_limit=int(
                schedule_report["selected_plan"]["comparison_limit"]
            ),
            decimals=_precision_decimals(snapshot.payload["precision"]),
        )
        neardup_artifact = cache.put(
            "neardup",
            neardup_key,
            {
                "stage": "neardup",
                "schema_version": "neardup-stage/1.0",
                "input_digest": dataset_digest,
                "upstream_artifact_hash": quality_artifact["content_hash"],
                "config_version": snapshot.stage_version("neardup"),
                "sampling_seed": int(snapshot.payload["seed"]),
                **neardup_result,
            },
        )

    consistency_key = stage_cache_key(
        stage="consistency",
        input_digest=dataset_digest,
        upstream_artifact_hash=neardup_artifact["content_hash"],
        config_version=snapshot.stage_version("consistency"),
        seed=int(snapshot.payload["seed"]),
        extra=_cache_key_extra(
            active_pair_ids=active_pair_ids, schedule_report=schedule_report
        ),
        schema_version="consistency-cache/1.0",
    )
    consistency_artifact, consistency_hit = _stage_from_cache(
        cache, "consistency", consistency_key
    )
    if consistency_artifact is None:
        consistency_result = compute_consistency(
            active_pairs, snapshot.payload["consistency"]
        )
        consistency_artifact = cache.put(
            "consistency",
            consistency_key,
            {
                "stage": "consistency",
                "schema_version": "consistency-stage/1.0",
                "input_digest": dataset_digest,
                "upstream_artifact_hash": neardup_artifact["content_hash"],
                "config_version": snapshot.stage_version("consistency"),
                "sampling_seed": int(snapshot.payload["seed"]),
                **consistency_result,
            },
        )

    sampling_key = stage_cache_key(
        stage="sampling",
        input_digest=dataset_digest,
        upstream_artifact_hash=consistency_artifact["content_hash"],
        config_version=snapshot.stage_version("sampling"),
        seed=int(snapshot.payload["seed"]),
        extra=_cache_key_extra(
            active_pair_ids=active_pair_ids, schedule_report=schedule_report
        ),
        schema_version="sampling-cache/1.0",
    )
    sampling_artifact, sampling_hit = _stage_from_cache(cache, "sampling", sampling_key)
    if sampling_artifact is None:
        sampling_result = compute_sampling(
            active_pairs,
            quality_records_by_id,
            neardup_artifact,
            consistency_artifact,
            snapshot.payload["sampling"],
            seed=int(snapshot.payload["seed"]),
        )
        sampling_artifact = cache.put(
            "sampling",
            sampling_key,
            {
                "stage": "sampling",
                "schema_version": "sampling-stage/1.0",
                "input_digest": dataset_digest,
                "upstream_artifact_hash": consistency_artifact["content_hash"],
                "config_version": snapshot.stage_version("sampling"),
                "sampling_seed": int(snapshot.payload["seed"]),
                **sampling_result,
            },
        )

    for batch in schedule_report["batches"]:
        batch.pop("_pairs", None)
    schedule_report_artifact = sealed(schedule_report)
    manifest_key = stage_cache_key(
        stage="manifest",
        input_digest=dataset_digest,
        upstream_artifact_hash=sampling_artifact["content_hash"],
        config_version=snapshot.stage_version("manifest"),
        seed=int(snapshot.payload["seed"]),
        extra={
            "config_version": snapshot.version,
            "config_checksum": snapshot.checksum,
            "schedule_report_hash": schedule_report_artifact["content_hash"],
            "stage_hashes": {
                "quality": quality_artifact["content_hash"],
                "neardup": neardup_artifact["content_hash"],
                "consistency": consistency_artifact["content_hash"],
                "sampling": sampling_artifact["content_hash"],
            },
        },
        schema_version="manifest-cache/1.0",
    )
    cached_manifest, manifest_hit = _stage_from_cache(cache, "manifest", manifest_key)

    stage_audit, incremental = _compute_stage_audit(
        previous=previous_state,
        snapshot=snapshot,
        input_pair_digests=pair_digest_map,
        all_pairs=pairs,
        active_pair_ids=active_pair_ids,
        schedule_report=schedule_report,
        qualidade_hits=quality_hits,
        qualidade_misses=quality_recomputed,
        neardup_hit=neardup_hit,
        consistency_hit=consistency_hit,
        sampling_hit=sampling_hit,
        manifest_hit=manifest_hit,
        neardup_artifact=neardup_artifact,
        seed=int(snapshot.payload["seed"]),
    )

    if cached_manifest is not None:
        manifest_artifact = sealed(cached_manifest)
    else:
        manifest_payload = _build_manifest_payload(
            pairs=pairs,
            pair_digest_map=pair_digest_map,
            dataset_digest=dataset_digest,
            snapshot=snapshot,
            schedule_report=schedule_report,
            schedule_report_artifact=schedule_report_artifact,
            quality_artifact=quality_artifact,
            neardup_artifact=neardup_artifact,
            consistency_artifact=consistency_artifact,
            sampling_artifact=sampling_artifact,
            errors_by_pair=errors_by_pair,
            active_pair_ids=active_pair_ids,
        )
        selfcheck = run_selfcheck(
            manifest_payload,
            quality_artifact,
            neardup_artifact,
            consistency_artifact,
            sampling_artifact,
            schedule_report,
            active_pair_ids=active_pair_ids,
            stage_audit=stage_audit,
        )
        manifest_payload["selfcheck"] = selfcheck
        if not selfcheck["ok"]:
            diagnostics = _build_diagnostics(
                snapshot=snapshot,
                dataset_digest=dataset_digest,
                manifest_artifact=None,
                manifest_file_hash=None,
                cache=cache,
                schedule_report=schedule_report,
                stage_audit=stage_audit,
                incremental=incremental,
                selfcheck=selfcheck,
                stage_hashes={
                    "quality": quality_artifact["content_hash"],
                    "neardup": neardup_artifact["content_hash"],
                    "consistency": consistency_artifact["content_hash"],
                    "sampling": sampling_artifact["content_hash"],
                },
                business_codes=[E_SELFCHECK_FAILED],
            )
            atomic_write_json(out_path / "diagnostics.json", diagnostics)
            raise BusinessError(
                E_SELFCHECK_FAILED,
                "cross-module self-check failed",
                details={"violations": selfcheck["violations"]},
            )
        manifest_artifact = cache.put("manifest", manifest_key, manifest_payload)

    if not manifest_artifact.get("selfcheck", {}).get("ok", False):
        raise BusinessError(
            E_SELFCHECK_FAILED,
            "cached manifest self-check is not valid",
            details={"violations": manifest_artifact.get("selfcheck", {}).get("violations")},
        )

    for name, artifact in (
        ("quality", quality_artifact),
        ("neardup", neardup_artifact),
        ("consistency", consistency_artifact),
        ("sampling", sampling_artifact),
        ("schedule_report", schedule_report_artifact),
    ):
        atomic_write_json(out_path / f"{name}.json", artifact, canonical=True)
    publish_run_manifest(manifest_path, manifest_artifact)
    manifest_file_hash = file_sha256(manifest_path)
    stage_hashes = {
        "quality": quality_artifact["content_hash"],
        "neardup": neardup_artifact["content_hash"],
        "consistency": consistency_artifact["content_hash"],
        "sampling": sampling_artifact["content_hash"],
        "schedule_report": schedule_report_artifact["content_hash"],
        "manifest": manifest_artifact["content_hash"],
    }
    atomic_write_json(out_path / "hashes.json", stage_hashes, canonical=True)
    diagnostics = _build_diagnostics(
        snapshot=snapshot,
        dataset_digest=dataset_digest,
        manifest_artifact=manifest_artifact,
        manifest_file_hash=manifest_file_hash,
        cache=cache,
        schedule_report=schedule_report,
        stage_audit=stage_audit,
        incremental=incremental,
        selfcheck=manifest_artifact["selfcheck"],
        stage_hashes=stage_hashes,
        business_codes=manifest_artifact.get("business_codes", []),
    )
    atomic_write_json(out_path / "diagnostics.json", diagnostics)

    _write_state(
        cache_root_path,
        {
            "schema_version": "workbench-state/1.0",
            "config": {"version": snapshot.version, "checksum": snapshot.checksum},
            "stage_config_versions": {
                stage: snapshot.stage_version(stage)
                for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
            },
            "input_digest": dataset_digest,
            "pair_input_digests": pair_digest_map,
            "pair_admission": {
                pair_id: path for pair_id, path in schedule_report["pair_paths"].items()
            },
            "prompt_digests": {
                str(pair["pair_id"]): content_hash(
                    normalize_text(str(pair["prompt"]))
                )
                for pair in pairs
            },
            "cluster_by_pair": {
                pair_id: assignment["cluster_id"]
                for pair_id, assignment in neardup_artifact["assignments"].items()
            },
            "cluster_members": {
                cluster["cluster_id"]: cluster["members"]
                for cluster in neardup_artifact["clusters"]
            },
            "selected_pair_ids": sampling_artifact["selected_pair_ids"],
            "manifest_content_hash": manifest_artifact["content_hash"],
            "stage_hashes": stage_hashes,
            "sampling_seed": int(snapshot.payload["seed"]),
        },
    )
    return {
        "manifest": manifest_artifact,
        "manifest_file": str(manifest_path),
        "manifest_file_sha256": manifest_file_hash,
        "stage_hashes": stage_hashes,
        "diagnostics": diagnostics,
        "manifest_hit": manifest_hit,
    }


def _precision_decimals(precision: str) -> int:
    return 8 if precision == "fp64" else 8


def _build_manifest_payload(
    *,
    pairs: list[dict[str, Any]],
    pair_digest_map: dict[str, str],
    dataset_digest: str,
    snapshot: ConfigSnapshot,
    schedule_report: dict[str, Any],
    schedule_report_artifact: dict[str, Any],
    quality_artifact: dict[str, Any],
    neardup_artifact: dict[str, Any],
    consistency_artifact: dict[str, Any],
    sampling_artifact: dict[str, Any],
    errors_by_pair: dict[str, dict[str, Any]],
    active_pair_ids: list[str],
) -> dict[str, Any]:
    quality_map = {
        str(record["pair_id"]): record for record in quality_artifact["records"]
    }
    selected_map = {
        str(item["pair_id"]): item for item in sampling_artifact["sequence"]
    }
    exclusions = sampling_artifact.get("exclusions", {})
    active = set(active_pair_ids)
    records: list[dict[str, Any]] = []
    business_codes: set[str] = set()
    for pair in pairs:
        pair_id = str(pair["pair_id"])
        path = dict(schedule_report["pair_paths"][pair_id])
        quality = quality_map.get(pair_id)
        cluster = neardup_artifact["assignments"].get(pair_id)
        consistency = consistency_artifact["records"].get(pair_id)
        selected_detail = selected_map.get(pair_id)
        if pair_id in errors_by_pair:
            error = errors_by_pair[pair_id]
            admission = dict(path)
            admission["status"] = "worker_error"
            admission["code"] = error["code"]
            admission["stage"] = error["stage"]
            admission["execution_error"] = error
            business_codes.add(str(error["code"]))
        else:
            admission = path
            if path.get("code") and path["code"] != "OK":
                business_codes.add(str(path["code"]))

        if pair_id in active:
            if selected_detail is not None:
                pair_status = "sampled"
            elif consistency and consistency["conflicted"]:
                pair_status = "conflict"
            elif quality and not quality["pass"]:
                pair_status = "quality_failed"
            elif cluster and not cluster["is_representative"]:
                pair_status = "duplicate_excluded"
            else:
                pair_status = "eligible_not_selected"
        elif pair_id in errors_by_pair:
            pair_status = "worker_error"
        else:
            pair_status = "schedule_rejected"

        if consistency:
            business_codes.update(str(code) for code in consistency["codes"])
        sampling_record = {
            "selected": selected_detail is not None,
            "step": selected_detail["step"] if selected_detail else None,
            "quota_key": selected_detail["quota_key"] if selected_detail else None,
            "selection_hash": (
                selected_detail["selection_hash"] if selected_detail else None
            ),
            "exclusions": list(exclusions.get(pair_id, [])),
        }
        records.append(
            {
                "pair_id": pair_id,
                "input_digest": pair_digest_map[pair_id],
                "status": pair_status,
                "admission": admission,
                "quality": quality,
                "cluster": cluster,
                "consistency": consistency,
                "sampling": sampling_record,
            }
        )
    business_codes.update(
        str(code) for code in neardup_artifact.get("warnings", [])
    )
    counts = Counter(record["status"] for record in records)
    return {
        "schema_version": "manifest/1.0",
        "config": {"version": snapshot.version, "checksum": snapshot.checksum},
        "stage_config_versions": {
            stage: snapshot.stage_version(stage)
            for stage in ("quality", "neardup", "consistency", "sampling", "manifest")
        },
        "input_digest": dataset_digest,
        "input_pair_ids": [str(pair["pair_id"]) for pair in pairs],
        "sampling_seed": int(snapshot.payload["seed"]),
        "stage_hashes": {
            "quality": quality_artifact["content_hash"],
            "neardup": neardup_artifact["content_hash"],
            "consistency": consistency_artifact["content_hash"],
            "sampling": sampling_artifact["content_hash"],
            "schedule_report": schedule_report_artifact["content_hash"],
        },
        "schedule": {
            "plan_fingerprint": schedule_report["plan_fingerprint"],
            "schedule_report_hash": schedule_report_artifact["content_hash"],
            "selected_plan": schedule_report["selected_plan"],
            "adjudications": schedule_report["adjudications"],
            "degraded_grouping": schedule_report["degraded_grouping"],
            "rejected_codes": schedule_report["rejected_codes"],
        },
        "records": records,
        "status_counts": {key: counts[key] for key in sorted(counts)},
        "quota_stats": sampling_artifact["quota_stats"],
        "sampling_sequence": sampling_artifact["sequence"],
        "business_codes": sorted(business_codes),
        "selfcheck": {"ok": False, "violations": []},
    }


def _build_diagnostics(
    *,
    snapshot: ConfigSnapshot,
    dataset_digest: str,
    manifest_artifact: dict[str, Any] | None,
    manifest_file_hash: str | None,
    cache: ArtifactCache,
    schedule_report: dict[str, Any],
    stage_audit: dict[str, Any],
    incremental: dict[str, Any],
    selfcheck: dict[str, Any],
    stage_hashes: dict[str, Any],
    business_codes: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "diagnostics/1.0",
        "config": {"version": snapshot.version, "checksum": snapshot.checksum},
        "input_digest": dataset_digest,
        "manifest_content_hash": (
            manifest_artifact["content_hash"] if manifest_artifact else None
        ),
        "manifest_file_sha256": manifest_file_hash,
        "stage_hashes": stage_hashes,
        "schedule": {
            "plan_fingerprint": schedule_report["plan_fingerprint"],
            "selected_plan": schedule_report["selected_plan"],
            "adjudications": schedule_report["adjudications"],
            "degraded_grouping": schedule_report["degraded_grouping"],
            "rejected_codes": schedule_report["rejected_codes"],
        },
        "cache": cache.stats(),
        "stage_audit": stage_audit,
        "incremental": incremental,
        "selfcheck": selfcheck,
        "business_codes": sorted(set(str(code) for code in business_codes)),
    }
