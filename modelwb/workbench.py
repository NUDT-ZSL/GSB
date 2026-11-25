from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write,
    canonical_json_text,
    read_json,
    sha256_file,
    sha256_json,
)
from .domain import Manifest
from .errors import WorkbenchError, fail
from .invalidation import calculate_invalidation
from .loaders import Catalog, load_catalog, load_full_replacement
from .patching import (
    apply_patch,
    check_manifest_artifacts,
    first_directory_divergence,
    parse_patch,
)
from .storage import Storage, now_ms


DEFAULT_CLOCK_TOLERANCE = 50


def _crash_if(point: str) -> None:
    configured = os.environ.get("MODELWB_CRASH_AT", "")
    points = {item.strip() for item in configured.split(",") if item.strip()}
    if point in points:
        print(f"MODELWB_CRASH_AT={point}; terminating without cleanup", file=sys.stderr)
        sys.stderr.flush()
        os._exit(86)


def _operation_id(prefix: str, request: dict[str, Any]) -> str:
    return f"{prefix}-{sha256_json(request)[:24]}"


class Workbench:
    def __init__(self, db_path: str | Path):
        self.storage = Storage(db_path)

    # ------------------------------------------------------------------
    # Catalog loading
    # ------------------------------------------------------------------
    def load(
        self,
        *,
        input_dir: str | Path | None = None,
        v1_path: str | Path | None = None,
        v2_db_path: str | Path | None = None,
        v2_dir: str | Path | None = None,
        registries_path: str | Path | None = None,
        replicas_path: str | Path | None = None,
        baseline_version: str | None = None,
        op_id: str | None = None,
        clock_tolerance: int = DEFAULT_CLOCK_TOLERANCE,
    ) -> dict[str, Any]:
        paths = self._resolve_input_paths(
            input_dir=input_dir,
            v1_path=v1_path,
            v2_db_path=v2_db_path,
            v2_dir=v2_dir,
            registries_path=registries_path,
            replicas_path=replicas_path,
        )
        catalog = load_catalog(
            v1_path=paths["v1_path"],
            v2_db_path=paths["v2_db_path"],
            v2_dir=paths["v2_dir"],
            registries_path=paths["registries_path"],
        )
        replica_data = self._read_replica_file(paths["replicas_path"])
        selected_baseline = (
            baseline_version
            or replica_data.get("service_baseline")
            or self._infer_baseline(catalog, replica_data.get("replicas", []))
        )
        request = {
            "v1_path": _path_or_none(paths["v1_path"]),
            "v2_db_path": _path_or_none(paths["v2_db_path"]),
            "v2_dir": _path_or_none(paths["v2_dir"]),
            "registries_path": _path_or_none(paths["registries_path"]),
            "replicas_path": _path_or_none(paths["replicas_path"]),
            "baseline_version": selected_baseline,
            "input_digest": catalog.input_digest,
            "clock_tolerance": clock_tolerance,
        }
        operation_id = op_id or _operation_id("load", request)
        with self.storage.operation_lock():
            operation_id = self._effective_default_operation_id(
                operation_id,
                explicit=op_id,
            )
            return self._load_impl(request, operation_id)

    def _load_impl(self, request: dict[str, Any], op_id: str) -> dict[str, Any]:
        operation, _ = self.storage.begin_operation(op_id, "load", request)
        if operation["status"] == "committed":
            return operation["result"]
        if operation["status"] == "failed":
            fail(
                "INVALID_STATE",
                "operation previously failed; use a new operation id",
                op_id=op_id,
                previous_error=operation["error"],
            )
        _crash_if("after_prepare")
        try:
            catalog = load_catalog(
                v1_path=request.get("v1_path"),
                v2_db_path=request.get("v2_db_path"),
                v2_dir=request.get("v2_dir"),
                registries_path=request.get("registries_path"),
            )
            if request.get("input_digest") and catalog.input_digest != request["input_digest"]:
                fail(
                    "CONFLICT",
                    "input catalog changed while the load operation was prepared",
                    expected=request["input_digest"],
                    actual=catalog.input_digest,
                )
            replica_data = self._read_replica_file(request.get("replicas_path"))
            baseline_version = request.get("baseline_version")
            if not baseline_version:
                baseline_version = (
                    replica_data.get("service_baseline")
                    or self._infer_baseline(catalog, replica_data.get("replicas", []))
                )
            baseline = catalog.manifest(baseline_version)
            if baseline.tombstone:
                fail(
                    "INVALID_STATE",
                    "baseline version is a tombstone",
                    baseline_version=baseline_version,
                )
            replicas, caches = self._build_replicas_and_caches(
                catalog, replica_data, baseline_version
            )
            tolerance = int(request.get("clock_tolerance", DEFAULT_CLOCK_TOLERANCE))
            maximum_clock = max(manifest.logical_clock for manifest in catalog.all_versions)
            previous_watermark = int(self.storage.get_meta("clock_watermark", 0))
            self._check_clock(
                maximum_clock,
                previous_watermark,
                tolerance,
                operation="load",
            )
            config_binding = catalog.config_binding(baseline)
            semantic = {
                "kind": "catalog",
                "baseline_version": baseline.version,
                "input_digest": catalog.input_digest,
                "manifest": baseline.core_dict(),
                "config_binding": config_binding,
                "replica_ids": [replica["replica_id"] for replica in replicas],
                "cache_ids": [cache["cache_id"] for cache in caches],
            }
            snapshot_id = "cat-" + sha256_json(semantic)[:24]
            payload = dict(semantic)
            payload.update(
                {
                    "snapshot_id": snapshot_id,
                    "shard_chain": baseline.shard_chain(),
                    "directory_digest": baseline.directory_digest(),
                    "configs": self._config_snapshot(catalog, baseline),
                    "patch_chain": [],
                    "unified_view": catalog.unified_view(),
                }
            )
            self._write_snapshot_artifact(snapshot_id, payload)
            self.storage.set_step(
                op_id,
                "catalog_validated",
                "committed",
                {
                    "input_digest": catalog.input_digest,
                    "baseline_version": baseline.version,
                    "versions": len(catalog.all_versions),
                },
            )
            crashpoint = "load_before_commit"
            _crash_if(crashpoint)
            result = {
                "ok": True,
                "operation_id": op_id,
                "snapshot_id": snapshot_id,
                "baseline_version": baseline.version,
                "catalog_versions": len(catalog.all_versions),
                "active_versions": catalog.unified_view()["active_versions"],
                "tombstones": catalog.unified_view()["tombstones"],
                "replicas": [replica["replica_id"] for replica in replicas],
                "caches": len(caches),
                "clock_watermark": max(previous_watermark, maximum_clock),
            }
            with self.storage.transaction() as connection:
                self.storage.replace_catalog(catalog, conn=connection)
                self.storage.replace_replicas(replicas, conn=connection)
                self.storage.replace_caches(caches, conn=connection)
                self.storage.insert_snapshot(
                    snapshot_id=snapshot_id,
                    parent_snapshot_id=None,
                    kind="catalog",
                    version=baseline.version,
                    logical_clock=baseline.logical_clock,
                    generation=baseline.generation,
                    directory_digest=baseline.directory_digest(),
                    payload=payload,
                    conn=connection,
                )
                self.storage.set_meta("current_snapshot", snapshot_id, conn=connection)
                self.storage.set_meta("baseline_snapshot", snapshot_id, conn=connection)
                self.storage.set_meta("current_version", baseline.version, conn=connection)
                self.storage.set_meta("baseline_version", baseline.version, conn=connection)
                self.storage.set_meta("update_round", 0, conn=connection)
                self.storage.set_meta(
                    "clock_watermark",
                    max(previous_watermark, maximum_clock),
                    conn=connection,
                )
                self.storage.complete_operation(op_id, result, conn=connection)
            return result
        except WorkbenchError as exc:
            self.storage.fail_operation(op_id, exc.to_dict())
            raise

    # ------------------------------------------------------------------
    # Patch application
    # ------------------------------------------------------------------
    def patch(
        self,
        *,
        patch_path: str | Path,
        full_path: str | Path | None = None,
        op_id: str | None = None,
        clock_tolerance: int = DEFAULT_CLOCK_TOLERANCE,
    ) -> dict[str, Any]:
        patch_source = Path(patch_path).resolve()
        spec = parse_patch(patch_source)
        full_source = Path(full_path).resolve() if full_path else None
        request = {
            "patch_path": str(patch_source),
            "patch_sha256": spec.patch_sha256,
            "full_path": str(full_source) if full_source else None,
            "full_sha256": sha256_file(full_source) if full_source else None,
            "clock_tolerance": clock_tolerance,
        }
        operation_id = op_id or _operation_id("patch", request)
        with self.storage.operation_lock():
            operation_id = self._effective_default_operation_id(
                operation_id,
                explicit=op_id,
            )
            return self._patch_impl(request, operation_id)

    def _patch_impl(self, request: dict[str, Any], op_id: str) -> dict[str, Any]:
        operation, _ = self.storage.begin_operation(op_id, "patch", request)
        if operation["status"] == "committed":
            return operation["result"]
        if operation["status"] == "failed":
            fail(
                "INVALID_STATE",
                "operation previously failed; use a new operation id",
                op_id=op_id,
                previous_error=operation["error"],
            )
        _crash_if("after_prepare")
        try:
            spec = parse_patch(request["patch_path"])
            if spec.patch_sha256 != request["patch_sha256"]:
                fail(
                    "CONFLICT",
                    "patch content changed while the operation was prepared",
                    operation_patch_sha256=request["patch_sha256"],
                    current_patch_sha256=spec.patch_sha256,
                )
            catalog = self.storage.catalog()
            if not catalog.all_versions:
                fail("INVALID_STATE", "catalog is not loaded", hint="run load first")
            current = self.storage.current_snapshot()
            if current is None:
                fail("INVALID_STATE", "current snapshot pointer is missing")
            if current["version"] != spec.base_version:
                fail(
                    "INVALID_STATE",
                    "patch baseline does not match the current snapshot",
                    patch_base_version=spec.base_version,
                    current_version=current["version"],
                    current_snapshot_id=current["snapshot_id"],
                )
            base = catalog.manifest(spec.base_version)
            if base.tombstone:
                fail("INVALID_STATE", "patch baseline is tombstoned", version=base.version)
            if request.get("full_path"):
                if request.get("full_sha256") and sha256_file(request["full_path"]) != request["full_sha256"]:
                    fail(
                        "CONFLICT",
                        "full replacement file changed while the operation was prepared",
                        expected=request["full_sha256"],
                        actual=sha256_file(request["full_path"]),
                    )
                full_replacement = load_full_replacement(
                    request["full_path"],
                    catalog=catalog,
                    target_version=spec.target_version,
                )
            else:
                full_replacement = catalog.manifest(spec.target_version)
            try:
                target_artifact = catalog.manifest(spec.target_version, source_format="v2")
            except WorkbenchError:
                target_artifact = full_replacement
            health = check_manifest_artifacts(target_artifact)
            computation = apply_patch(
                base,
                full_replacement,
                spec,
                target_artifact_manifest=target_artifact,
            )
            target = computation.target
            if target.directory_digest() != target_artifact.directory_digest():
                divergence = first_directory_divergence(target, target_artifact)
                fail(
                    "PATCH_FULL_REPLACEMENT_MISMATCH",
                    "incremental target chain differs from the catalog target artifact",
                    first_divergence=divergence
                    or {
                        "field": "directory_digest",
                        "incremental": target.directory_digest(),
                        "catalog_artifact": target_artifact.directory_digest(),
                    },
                )
            target = target.with_overrides(
                source_path=target_artifact.source_path,
                source_dir=target_artifact.source_dir,
            )
            tolerance = int(request.get("clock_tolerance", DEFAULT_CLOCK_TOLERANCE))
            watermark = int(self.storage.get_meta("clock_watermark", 0))
            self._check_clock(target.logical_clock, watermark, tolerance, operation="patch")
            config_binding = catalog.config_binding(full_replacement)
            update_round = int(self.storage.get_meta("update_round", 0)) + 1
            parent_payload = current.get("payload", {})
            patch_chain = list(parent_payload.get("patch_chain", []))
            patch_chain.append(
                {
                    "patch_id": spec.patch_id,
                    "patch_sha256": spec.patch_sha256,
                    "target_directory_digest": target.directory_digest(),
                }
            )
            semantic = {
                "kind": "patch",
                "parent_snapshot_id": current["snapshot_id"],
                "version": target.version,
                "directory_digest": target.directory_digest(),
                "manifest": target.core_dict(),
                "config_binding": config_binding,
                "patch_chain": patch_chain,
                "update_round": update_round,
                "full_replacement_directory_digest": full_replacement.directory_digest(),
            }
            snapshot_id = "snap-" + sha256_json(semantic)[:24]
            payload = dict(semantic)
            payload.update(
                {
                    "snapshot_id": snapshot_id,
                    "shard_chain": target.shard_chain(),
                    "source_dir": target.source_dir,
                    "configs": self._config_snapshot(catalog, full_replacement),
                    "patch_summary": computation.summary(),
                }
            )
            self._write_snapshot_artifact(snapshot_id, payload)
            self.storage.set_step(
                op_id,
                "patch_compared",
                "committed",
                computation.summary(),
            )
            caches = self.storage.list_caches()
            replicas = self.storage.list_replicas()
            invalidation = calculate_invalidation(
                catalog,
                base=base,
                target=full_replacement,
                changed_shards=computation.changed_shards,
                caches=caches,
                replicas=replicas,
                operation_id=op_id,
            )
            self.storage.set_step(
                op_id,
                "invalidation_calculated",
                "committed",
                invalidation.to_dict(),
            )
            _crash_if("patch_before_commit")
            release_id = "run-" + sha256_json(
                {
                    "op_id": op_id,
                    "baseline_version": base.version,
                    "target_version": target.version,
                    "baseline_snapshot_id": current["snapshot_id"],
                    "target_snapshot_id": snapshot_id,
                }
            )[:24]
            result = {
                "ok": True,
                "operation_id": op_id,
                "snapshot_id": snapshot_id,
                "release_id": release_id,
                "base_version": base.version,
                "target_version": target.version,
                "directory_digest": target.directory_digest(),
                "full_replacement_directory_digest": full_replacement.directory_digest(),
                "patch_chain": patch_chain,
                "update_round": update_round,
                "unchanged_shards": computation.unchanged_shard_count,
                "changed_shards": computation.changed_shards,
                "invalidation": invalidation.to_dict(),
            }
            with self.storage.transaction() as connection:
                self.storage.insert_snapshot(
                    snapshot_id=snapshot_id,
                    parent_snapshot_id=current["snapshot_id"],
                    kind="patch",
                    version=target.version,
                    logical_clock=target.logical_clock,
                    generation=target.generation,
                    directory_digest=target.directory_digest(),
                    payload=payload,
                    conn=connection,
                )
                self.storage.invalidate_caches(
                    invalidation.invalidated_cache_ids,
                    invalidation.event_id,
                    conn=connection,
                )
                self.storage.record_invalidation(
                    event_id=invalidation.event_id,
                    operation_id=op_id,
                    reason="patch",
                    roots=invalidation.roots,
                    affected_replicas=invalidation.affected_replica_ids,
                    invalidated_caches=invalidation.invalidated_cache_ids,
                    reusable_caches=invalidation.reusable_cache_ids,
                    details=invalidation.details,
                    conn=connection,
                )
                self.storage.create_release(
                    run_id=release_id,
                    operation_id=op_id,
                    target_version=target.version,
                    baseline_version=base.version,
                    baseline_snapshot_id=current["snapshot_id"],
                    target_snapshot_id=snapshot_id,
                    stages=[],
                    status="pending_warmup",
                    conn=connection,
                )
                self.storage.set_meta("current_snapshot", snapshot_id, conn=connection)
                self.storage.set_meta("current_version", target.version, conn=connection)
                self.storage.set_meta("update_round", update_round, conn=connection)
                self.storage.set_meta(
                    "clock_watermark",
                    max(watermark, target.logical_clock),
                    conn=connection,
                )
                self.storage.complete_operation(op_id, result, conn=connection)
            _crash_if("patch_after_commit")
            return result
        except WorkbenchError as exc:
            self.storage.fail_operation(op_id, exc.to_dict())
            raise

    def verify_patch(
        self,
        *,
        patch_path: str | Path,
        full_path: str | Path | None = None,
        base_version: str | None = None,
    ) -> dict[str, Any]:
        spec = parse_patch(patch_path)
        catalog = self.storage.catalog()
        if not catalog.all_versions:
            fail("INVALID_STATE", "catalog is not loaded", hint="run load first")
        selected_base = base_version or spec.base_version
        base = catalog.manifest(selected_base)
        full = (
            load_full_replacement(full_path, catalog=catalog, target_version=spec.target_version)
            if full_path
            else catalog.manifest(spec.target_version)
        )
        try:
            target_artifact = catalog.manifest(spec.target_version, source_format="v2")
        except WorkbenchError:
            target_artifact = full
        check_manifest_artifacts(target_artifact)
        computation = apply_patch(base, full, spec, target_artifact_manifest=target_artifact)
        return {
            "ok": True,
            "matches_full_replacement": True,
            **computation.summary(),
        }

    # ------------------------------------------------------------------
    # Gray release
    # ------------------------------------------------------------------
    def gray(
        self,
        *,
        target_version: str,
        stages: Iterable[int | str],
        run_id: str | None = None,
        fail_replica: str | None = None,
        op_id: str | None = None,
    ) -> dict[str, Any]:
        parsed_stages = _validate_stages(stages)
        release = self.storage.get_release(run_id) if run_id else self.storage.latest_release(
            target_version=target_version
        )
        if release is None:
            fail("NOT_FOUND", "no release run exists for target version", target_version=target_version)
        if release["target_version"] != target_version:
            fail(
                "INVALID_STATE",
                "release target version does not match request",
                release_target=release["target_version"],
                requested_target=target_version,
            )
        request = {
            "run_id": release["run_id"],
            "target_version": target_version,
            "stages": parsed_stages,
            "fail_replica": fail_replica,
        }
        operation_id = op_id or _operation_id("gray", request)
        with self.storage.operation_lock():
            return self._gray_impl(request, operation_id)

    def _gray_impl(self, request: dict[str, Any], op_id: str) -> dict[str, Any]:
        operation, _ = self.storage.begin_operation(op_id, "gray", request)
        if operation["status"] == "committed":
            return operation["result"]
        if operation["status"] == "failed":
            fail(
                "INVALID_STATE",
                "operation previously failed; use rollback or a new release",
                op_id=op_id,
                previous_error=operation["error"],
            )
        _crash_if("after_prepare")
        try:
            run_id = request["run_id"]
            stages = _validate_stages(request["stages"])
            fail_replica = request.get("fail_replica")
            release = self.storage.get_release(run_id)
            if release["status"] in ("rolled_back", "rolling_back"):
                fail(
                    "INVALID_STATE",
                    "cannot advance gray release while or after rollback",
                    status=release["status"],
                    run_id=run_id,
                )
            if release["status"] == "failed":
                fail(
                    "INVALID_STATE",
                    "release is marked failed; execute rollback",
                    run_id=run_id,
                )
            catalog = self.storage.catalog()
            target_artifact = catalog.manifest(release["target_version"])
            target_snapshot = self.storage.get_snapshot(release["target_snapshot_id"])
            if target_snapshot["version"] != target_artifact.version:
                fail(
                    "INVALID_STATE",
                    "target snapshot does not match catalog target version",
                    snapshot_version=target_snapshot["version"],
                    target_version=target_artifact.version,
                )
            if target_snapshot["directory_digest"] != target_artifact.directory_digest():
                fail(
                    "INVALID_STATE",
                    "target snapshot digest does not match the catalog target artifact",
                    snapshot_directory_digest=target_snapshot["directory_digest"],
                    target_directory_digest=target_artifact.directory_digest(),
                )
            try:
                health = check_manifest_artifacts(target_artifact)
            except WorkbenchError as exc:
                self.storage.record_health(
                    target_artifact.version,
                    target_artifact.directory_digest(),
                    False,
                    exc.to_dict(),
                )
                self.storage.update_release(
                    run_id,
                    status="failed",
                    current_stage=release["current_stage"],
                    current_ratio=release["current_ratio"],
                )
                raise
            self.storage.record_health(
                target_artifact.version,
                target_artifact.directory_digest(),
                True,
                health,
            )
            self.storage.set_release_stages(run_id, stages)
            config_binding = catalog.config_binding(target_artifact)
            target_binding = {
                "version": target_artifact.version,
                "snapshot_id": target_snapshot["snapshot_id"],
                "directory_digest": target_artifact.directory_digest(),
                "config_binding": config_binding,
                "patch_chain": target_snapshot["payload"].get("patch_chain", []),
            }
            candidates = [
                replica
                for replica in self.storage.list_replicas()
                if replica["baseline_version"] == release["baseline_version"]
            ]
            if not candidates:
                fail(
                    "INVALID_STATE",
                    "no replicas are eligible for this release",
                    baseline_version=release["baseline_version"],
                )
            candidate_ids = [replica["replica_id"] for replica in candidates]
            if fail_replica is not None and fail_replica not in candidate_ids:
                fail(
                    "NOT_FOUND",
                    "fail_replica is not eligible for this release",
                    replica_id=fail_replica,
                    eligible=candidate_ids,
                )
            for replica in candidate_ids:
                step_name = f"warm:{replica}"
                existing_steps = {
                    step["step"]: step for step in self.storage.get_operation(op_id)["steps"]
                }
                if existing_steps.get(step_name, {}).get("status") == "committed":
                    continue
                current_replica = self.storage.get_replica(replica)
                if (
                    current_replica["stage"] in ("warmed", "gray")
                    and current_replica["version_binding"] == target_artifact.version
                ):
                    self.storage.set_step(op_id, step_name, "committed", {"reused": True})
                    continue
                self.storage.update_replica(
                    replica,
                    stage="pending",
                    version_binding=release["baseline_version"],
                    shard_chain_digest=self.storage.get_replica(replica)["shard_chain_digest"],
                    config_binding=self.storage.get_replica(replica)["config_binding"],
                    target_version=target_artifact.version,
                    health={"healthy": False, "phase": "pending_target_load"},
                )
                self.storage.update_replica(
                    replica,
                    stage="loading",
                    version_binding=target_artifact.version,
                    shard_chain_digest=target_artifact.directory_digest(),
                    config_binding=config_binding,
                    target_version=target_artifact.version,
                    health={"healthy": False, "phase": "loading"},
                )
                if fail_replica == replica:
                    baseline = catalog.manifest(release["baseline_version"])
                    baseline_binding = catalog.config_binding(baseline)
                    self.storage.update_replica(
                        replica,
                        stage="rollback",
                        version_binding=baseline.version,
                        shard_chain_digest=baseline.directory_digest(),
                        config_binding=baseline_binding,
                        target_version=None,
                        health={
                            "healthy": False,
                            "phase": "warmup",
                            "simulated": True,
                            "reason": "injected shard verification failure",
                        },
                    )
                    self.storage.update_release(
                        run_id,
                        status="failed",
                        current_stage=release["current_stage"],
                        current_ratio=release["current_ratio"],
                    )
                    self.storage.set_step(
                        op_id,
                        step_name,
                        "failed",
                        {"replica_id": replica, "reason": "injected digest failure"},
                    )
                    fail(
                        "SHARD_DIGEST_MISMATCH",
                        "prewarm digest verification failed; replica cannot enter gray",
                        replica_id=replica,
                        target_version=target_artifact.version,
                        simulated=True,
                    )
                self.storage.update_replica(
                    replica,
                    stage="warmed",
                    version_binding=target_artifact.version,
                    shard_chain_digest=target_artifact.directory_digest(),
                    config_binding=config_binding,
                    target_version=target_artifact.version,
                    health=health,
                )
                self.storage.set_step(
                    op_id,
                    step_name,
                    "committed",
                    {
                        "replica_id": replica,
                        "stage": "warmed",
                        "directory_digest": target_artifact.directory_digest(),
                    },
                )
            _crash_if("gray_after_warmup")
            for stage_index, ratio in enumerate(stages):
                if self.storage.stage_exists(run_id, stage_index):
                    continue
                release = self.storage.get_release(run_id)
                if release["status"] in ("rolled_back", "rolling_back", "failed"):
                    fail(
                        "INVALID_STATE",
                        "release changed state while gray advancement was running",
                        status=release["status"],
                    )
                current_health = check_manifest_artifacts(target_artifact)
                current_target = self.storage.get_snapshot(release["target_snapshot_id"])
                if current_target["directory_digest"] != target_artifact.directory_digest():
                    fail(
                        "INVALID_STATE",
                        "persisted target snapshot changed before gray stage",
                        stage_index=stage_index,
                    )
                replicas = self.storage.list_replicas()
                eligible = [
                    replica
                    for replica in replicas
                    if replica["replica_id"] in candidate_ids
                    and replica["stage"] in ("warmed", "gray")
                    and replica["version_binding"] == target_artifact.version
                ]
                if len(eligible) != len(candidate_ids):
                    fail(
                        "INVALID_STATE",
                        "not all target replicas completed prewarm before gray advancement",
                        eligible=[replica["replica_id"] for replica in eligible],
                        expected=candidate_ids,
                    )
                eligible.sort(key=lambda item: item["replica_id"])
                desired_count = (
                    len(eligible)
                    if ratio == 100
                    else max(1, math.ceil(len(eligible) * ratio / 100.0))
                )
                selected_ids = [replica["replica_id"] for replica in eligible[:desired_count]]
                status = "gray_complete" if ratio == 100 else "gray"
                decision = {
                    "target_version": target_artifact.version,
                    "ratio": ratio,
                    "previous_ratio": release["current_ratio"],
                    "basis": "target health passed, persisted snapshot matched, prewarm complete",
                    "health_directory_digest": current_health["directory_digest"],
                    "target_snapshot_id": current_target["snapshot_id"],
                    "deterministic_assignment": "sorted replica ids, ceil(ratio * candidate_count)",
                    "eligible_replicas": candidate_ids,
                    "entered_replicas": selected_ids,
                }
                with self.storage.transaction() as connection:
                    for replica_id in selected_ids:
                        connection.execute(
                            "UPDATE wt_replicas SET stage='gray', updated_at=? WHERE replica_id=?",
                            (now_ms(), replica_id),
                        )
                    self.storage.record_gray_stage(
                        run_id=run_id,
                        stage_index=stage_index,
                        ratio=ratio,
                        replicas=selected_ids,
                        decision=decision,
                        conn=connection,
                    )
                    self.storage.update_release(
                        run_id,
                        status=status,
                        current_stage=stage_index,
                        current_ratio=ratio,
                        conn=connection,
                    )
                    self.storage.set_step(
                        op_id,
                        f"gray_stage:{stage_index}",
                        "committed",
                        decision,
                        conn=connection,
                    )
                _crash_if(f"gray_after_stage:{stage_index + 1}")
                _crash_if(f"gray_after_ratio:{ratio}")
            release = self.storage.get_release(run_id)
            result = {
                "ok": True,
                "operation_id": op_id,
                "run_id": run_id,
                "target_version": target_artifact.version,
                "baseline_version": release["baseline_version"],
                "current_stage": release["current_stage"],
                "current_ratio": release["current_ratio"],
                "status": release["status"],
                "stages": release["gray_stages"],
                "health": health,
                "warmed_replicas": candidate_ids,
            }
            with self.storage.transaction() as connection:
                self.storage.complete_operation(op_id, result, conn=connection)
            return result
        except WorkbenchError as exc:
            self.storage.fail_operation(op_id, exc.to_dict())
            raise

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------
    def rollback(
        self,
        *,
        run_id: str | None = None,
        reason: str = "failure",
        trigger: str = "operator",
        window_ms: int = 5000,
        op_id: str | None = None,
    ) -> dict[str, Any]:
        release = self.storage.get_release(run_id) if run_id else self.storage.latest_release()
        if release is None:
            fail("NOT_FOUND", "no release run exists to roll back")
        request = {
            "run_id": release["run_id"],
            "reason": reason,
            "trigger": trigger,
            "window_ms": int(window_ms),
        }
        operation_id = op_id or _operation_id("rollback", request)
        with self.storage.operation_lock():
            return self._rollback_impl(request, operation_id)

    def _rollback_impl(self, request: dict[str, Any], op_id: str) -> dict[str, Any]:
        operation, _ = self.storage.begin_operation(op_id, "rollback", request)
        if operation["status"] == "committed":
            return operation["result"]
        if operation["status"] == "failed":
            fail(
                "INVALID_STATE",
                "operation previously failed; use a new operation id",
                op_id=op_id,
                previous_error=operation["error"],
            )
        _crash_if("after_prepare")
        try:
            release = self.storage.get_release(request["run_id"])
            catalog = self.storage.catalog()
            baseline = catalog.manifest(release["baseline_version"])
            baseline_snapshot = self.storage.get_snapshot(release["baseline_snapshot_id"])
            baseline_binding = catalog.config_binding(baseline)
            if release["trigger"]:
                trigger_context = release["trigger"]
            else:
                gray_replicas = sorted(
                    {
                        replica["replica_id"]
                        for replica in self.storage.list_replicas()
                        if replica["stage"] == "gray"
                        and replica["version_binding"] == release["target_version"]
                    }
                )
                trigger_context = {
                    "source": request["trigger"],
                    "reason": request["reason"],
                    "current_stage": release["current_stage"],
                    "current_ratio": release["current_ratio"],
                    "gray_replicas": gray_replicas,
                    "operation_id": op_id,
                    "window_ms": request["window_ms"],
                    "deadline_ms": now_ms() + request["window_ms"],
                    "baseline_version": release["baseline_version"],
                    "target_version": release["target_version"],
                }
            ratios = _rollback_ratios(release)
            if trigger_context.get("rollback_steps"):
                steps = [
                    (int(step["from_ratio"]), int(step["to_ratio"]))
                    for step in trigger_context["rollback_steps"]
                ]
            elif len(ratios) == 1:
                steps = [(ratios[0], 0)]
            else:
                steps = list(zip(ratios, ratios[1:]))
            trigger_context["rollback_steps"] = [
                {"from_ratio": from_ratio, "to_ratio": to_ratio}
                for from_ratio, to_ratio in steps
            ]
            trigger_gray = set(trigger_context.get("gray_replicas", []))
            all_target_replicas = sorted(
                {
                    replica["replica_id"]
                    for replica in self.storage.list_replicas()
                    if replica["baseline_version"] == release["baseline_version"]
                }
                | trigger_gray
            )
            result_steps: list[dict[str, Any]] = []
            for step_index, (from_ratio, to_ratio) in enumerate(steps):
                if self.storage.rollback_step_exists(release["run_id"], step_index):
                    result_steps.append(
                        {
                            "step_index": step_index,
                            "from_ratio": from_ratio,
                            "to_ratio": to_ratio,
                            "reused": True,
                        }
                    )
                    continue
                final = to_ratio == 0
                decision = {
                    "trigger": trigger_context,
                    "step_index": step_index,
                    "from_ratio": from_ratio,
                    "to_ratio": to_ratio,
                    "gray_replicas": sorted(trigger_gray),
                    "replicas": all_target_replicas,
                    "final": final,
                }
                result = {
                    "ok": True,
                    "operation_id": op_id,
                    "run_id": release["run_id"],
                    "trigger": trigger_context,
                    "from_ratio": from_ratio,
                    "to_ratio": to_ratio,
                    "steps": result_steps
                    + [
                        {
                            "step_index": step_index,
                            "from_ratio": from_ratio,
                            "to_ratio": to_ratio,
                        }
                    ],
                    "restored_version": release["baseline_version"] if final else None,
                    "baseline_snapshot_id": release["baseline_snapshot_id"],
                }
                with self.storage.transaction() as connection:
                    if final:
                        for replica_id in all_target_replicas:
                            connection.execute(
                                "UPDATE wt_replicas SET stage='warmed', target_version=NULL,"
                                " version_binding=?, shard_chain_digest=?, config_binding=?,"
                                " health_json=?, updated_at=? WHERE replica_id=?",
                                (
                                    baseline.version,
                                    baseline.directory_digest(),
                                    baseline_binding,
                                    canonical_json_text(
                                        {
                                            "healthy": True,
                                            "phase": "rollback_complete",
                                            "version": baseline.version,
                                        }
                                    ),
                                    now_ms(),
                                    replica_id,
                                ),
                            )
                        self.storage.set_meta(
                            "current_snapshot",
                            release["baseline_snapshot_id"],
                            conn=connection,
                        )
                        self.storage.set_meta(
                            "current_version", baseline.version, conn=connection
                        )
                    else:
                        for replica_id in trigger_gray | set(all_target_replicas):
                            connection.execute(
                                "UPDATE wt_replicas SET stage='rollback', updated_at=?"
                                " WHERE replica_id=?",
                                (now_ms(), replica_id),
                            )
                    self.storage.record_rollback_step(
                        run_id=release["run_id"],
                        step_index=step_index,
                        from_ratio=from_ratio,
                        to_ratio=to_ratio,
                        replicas=all_target_replicas,
                        decision=decision,
                        conn=connection,
                    )
                    self.storage.update_release(
                        release["run_id"],
                        status="rolled_back" if final else "rolling_back",
                        current_stage=-1 if final else release["current_stage"],
                        current_ratio=to_ratio,
                        trigger=trigger_context,
                        conn=connection,
                    )
                    self.storage.set_step(
                        op_id,
                        f"rollback_step:{step_index}",
                        "committed",
                        decision,
                        conn=connection,
                    )
                    if final:
                        self.storage.complete_operation(op_id, result, conn=connection)
                result_steps.append(
                    {
                        "step_index": step_index,
                        "from_ratio": from_ratio,
                        "to_ratio": to_ratio,
                    }
                )
                _crash_if(f"rollback_after_step:{step_index + 1}")
                _crash_if(f"rollback_after_ratio:{to_ratio}")
            final_result = self.storage.get_operation(op_id)
            if final_result["status"] == "committed":
                return final_result["result"]
            result = {
                "ok": True,
                "operation_id": op_id,
                "run_id": release["run_id"],
                "trigger": trigger_context,
                "steps": result_steps,
                "restored_version": baseline.version,
                "baseline_snapshot_id": release["baseline_snapshot_id"],
            }
            with self.storage.transaction() as connection:
                self.storage.set_meta(
                    "current_snapshot", release["baseline_snapshot_id"], conn=connection
                )
                self.storage.set_meta("current_version", baseline.version, conn=connection)
                self.storage.update_release(
                    release["run_id"],
                    status="rolled_back",
                    current_stage=-1,
                    current_ratio=0,
                    trigger=trigger_context,
                    conn=connection,
                )
                self.storage.complete_operation(op_id, result, conn=connection)
            return result
        except WorkbenchError as exc:
            self.storage.fail_operation(op_id, exc.to_dict())
            raise

    # ------------------------------------------------------------------
    # Request binding and recovery
    # ------------------------------------------------------------------
    def start_request(self, request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id:
            fail("VALIDATION_ERROR", "request_id must be a non-empty string")
        with self.storage.operation_lock():
            try:
                return self.storage.get_request(request_id)
            except WorkbenchError as exc:
                if exc.code != "NOT_FOUND":
                    raise
            release = self.storage.latest_release()
            baseline = self.storage.baseline_snapshot()
            if baseline is None:
                fail("INVALID_STATE", "baseline snapshot pointer is missing")
            selected_snapshot = baseline
            ratio = 0
            run_id = None
            if release is not None:
                ratio = release["current_ratio"] if release["status"] in ("gray", "gray_complete") else 0
                run_id = release["run_id"]
                if ratio > 0:
                    bucket = int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8], 16) % 10000
                    if bucket < ratio * 100:
                        selected_snapshot = self.storage.get_snapshot(release["target_snapshot_id"])
            binding = self._binding_from_snapshot(selected_snapshot)
            return self.storage.insert_request(
                request_id=request_id,
                run_id=run_id,
                model_version=binding["version"],
                snapshot_id=selected_snapshot["snapshot_id"],
                ratio_at_start=ratio,
                binding=binding,
            )

    def finish_request(self, request_id: str) -> dict[str, Any]:
        with self.storage.operation_lock():
            return self.storage.finish_request(request_id)

    def resume(self, op_id: str | None = None) -> dict[str, Any]:
        if op_id is not None:
            operation = self.storage.get_operation(op_id)
            if operation["status"] == "committed":
                return {"ok": True, "resumed": [], "already_committed": op_id, "result": operation["result"]}
            if operation["status"] == "failed":
                fail(
                    "INVALID_STATE",
                    "cannot resume a failed operation",
                    op_id=op_id,
                    error=operation["error"],
                )
            operations = [operation]
        else:
            operations = self.storage.incomplete_operations()
        outcomes: list[dict[str, Any]] = []
        for operation in operations:
            try:
                with self.storage.operation_lock():
                    request = operation["request"]
                    if operation["kind"] == "load":
                        result = self._load_impl(request, operation["op_id"])
                    elif operation["kind"] == "patch":
                        result = self._patch_impl(request, operation["op_id"])
                    elif operation["kind"] == "gray":
                        result = self._gray_impl(request, operation["op_id"])
                    elif operation["kind"] == "rollback":
                        result = self._rollback_impl(request, operation["op_id"])
                    else:
                        fail("INVALID_STATE", "unknown operation kind", kind=operation["kind"])
                outcomes.append(
                    {
                        "op_id": operation["op_id"],
                        "kind": operation["kind"],
                        "status": "committed",
                        "result": result,
                    }
                )
            except WorkbenchError as exc:
                outcomes.append(
                    {
                        "op_id": operation["op_id"],
                        "kind": operation["kind"],
                        "status": "error",
                        "error": exc.to_dict()["error"],
                    }
                )
        return {"ok": all(item["status"] == "committed" for item in outcomes), "resumed": outcomes}

    def status(self) -> dict[str, Any]:
        status = self.storage.status()
        catalog = self.storage.catalog()
        if catalog.all_versions:
            status["versions"] = catalog.unified_view()
        else:
            status["versions"] = {"versions": [], "active_versions": [], "tombstones": []}
        return status

    def versions(self) -> dict[str, Any]:
        catalog = self.storage.catalog()
        if not catalog.all_versions:
            fail("INVALID_STATE", "catalog is not loaded")
        return catalog.unified_view()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_input_paths(
        self,
        *,
        input_dir: str | Path | None,
        v1_path: str | Path | None,
        v2_db_path: str | Path | None,
        v2_dir: str | Path | None,
        registries_path: str | Path | None,
        replicas_path: str | Path | None,
    ) -> dict[str, Path | None]:
        resolved: dict[str, Path | None] = {
            "v1_path": Path(v1_path).resolve() if v1_path else None,
            "v2_db_path": Path(v2_db_path).resolve() if v2_db_path else None,
            "v2_dir": Path(v2_dir).resolve() if v2_dir else None,
            "registries_path": Path(registries_path).resolve() if registries_path else None,
            "replicas_path": Path(replicas_path).resolve() if replicas_path else None,
        }
        if input_dir is not None:
            root = Path(input_dir).resolve()
            if resolved["v1_path"] is None and (root / "v1_manifest.json").exists():
                resolved["v1_path"] = (root / "v1_manifest.json").resolve()
            if resolved["v2_db_path"] is None and (root / "v2" / "meta.db").exists():
                resolved["v2_db_path"] = (root / "v2" / "meta.db").resolve()
            if resolved["v2_dir"] is None and (root / "v2").exists():
                resolved["v2_dir"] = (root / "v2").resolve()
            if resolved["registries_path"] is None and (root / "registries.json").exists():
                resolved["registries_path"] = (root / "registries.json").resolve()
            if resolved["replicas_path"] is None and (root / "replicas.json").exists():
                resolved["replicas_path"] = (root / "replicas.json").resolve()
        if resolved["v2_db_path"] is not None and resolved["v2_dir"] is None:
            resolved["v2_dir"] = resolved["v2_db_path"].parent
        return resolved

    def _read_replica_file(self, path: str | Path | None) -> dict[str, Any]:
        if path is None:
            return {"service_baseline": None, "replicas": []}
        try:
            raw = read_json(path)
        except (OSError, ValueError) as exc:
            fail("IO_ERROR", "cannot read replicas file", path=str(path), detail=str(exc))
        if isinstance(raw, list):
            return {"service_baseline": None, "replicas": raw}
        if not isinstance(raw, dict):
            fail("VALIDATION_ERROR", "replicas file must be an object or list", path=str(path))
        replicas = raw.get("replicas", [])
        if not isinstance(replicas, list):
            fail("VALIDATION_ERROR", "replicas must be a list", path=str(path))
        return {
            "service_baseline": raw.get("service_baseline", raw.get("baseline_version")),
            "replicas": replicas,
        }

    def _infer_baseline(self, catalog: Catalog, replicas: list[Any]) -> str:
        counts: dict[str, int] = {}
        for replica in replicas:
            if isinstance(replica, dict) and isinstance(replica.get("baseline_version"), str):
                counts[replica["baseline_version"]] = counts.get(replica["baseline_version"], 0) + 1
        if counts:
            return max(counts, key=lambda version: (counts[version], version))
        candidates = [
            manifest
            for manifest in catalog.all_versions
            if not manifest.tombstone
        ]
        if not candidates:
            fail("INVALID_STATE", "catalog has no active version to use as baseline")
        return max(candidates, key=lambda manifest: (manifest.absolute_updated_ms, manifest.logical_clock)).version

    def _build_replicas_and_caches(
        self,
        catalog: Catalog,
        replica_data: dict[str, Any],
        baseline_version: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw_replicas = replica_data.get("replicas", [])
        if not raw_replicas:
            raw_replicas = [{"replica_id": "replica-0", "baseline_version": baseline_version}]
        replicas: list[dict[str, Any]] = []
        caches: list[dict[str, Any]] = []
        replica_ids: set[str] = set()
        cache_ids: set[str] = set()
        for index, raw in enumerate(raw_replicas):
            if not isinstance(raw, dict):
                fail("VALIDATION_ERROR", "replica entry must be an object", index=index)
            replica_id = raw.get("replica_id", raw.get("id"))
            if not isinstance(replica_id, str) or not replica_id:
                fail("VALIDATION_ERROR", "replica_id is required", index=index)
            if replica_id in replica_ids:
                fail("VALIDATION_ERROR", "replica_id is duplicated", replica_id=replica_id)
            replica_ids.add(replica_id)
            bound_version = raw.get("baseline_version", raw.get("version", baseline_version))
            if not isinstance(bound_version, str):
                fail("VALIDATION_ERROR", "replica baseline_version must be a string", replica_id=replica_id)
            manifest = catalog.manifest(bound_version)
            if manifest.tombstone:
                fail(
                    "INVALID_STATE",
                    "replica cannot bind to a tombstoned version",
                    replica_id=replica_id,
                    version=bound_version,
                )
            config_binding = catalog.config_binding(manifest)
            replicas.append(
                {
                    "replica_id": replica_id,
                    "baseline_version": bound_version,
                    "target_version": None,
                    "stage": "warmed",
                    "version_binding": bound_version,
                    "shard_chain_digest": manifest.directory_digest(),
                    "config_binding": config_binding,
                    "health": {
                        "healthy": True,
                        "phase": "loaded_baseline",
                        "directory_digest": manifest.directory_digest(),
                    },
                }
            )
            for cache_index, cache_raw in enumerate(raw.get("caches", [])):
                if not isinstance(cache_raw, dict):
                    cache_raw = {}
                cache_version = cache_raw.get("version", bound_version)
                cache_manifest = catalog.manifest(cache_version)
                if cache_manifest.tombstone:
                    fail(
                        "INVALID_STATE",
                        "cache cannot bind to a tombstoned version",
                        replica_id=replica_id,
                        version=cache_version,
                    )
                cache_id = cache_raw.get(
                    "cache_id",
                    f"{replica_id}-{cache_raw.get('kind', 'warmup')}-{cache_index}",
                )
                if not isinstance(cache_id, str) or not cache_id:
                    fail("VALIDATION_ERROR", "cache_id must be a non-empty string")
                if cache_id in cache_ids:
                    fail("VALIDATION_ERROR", "cache_id is duplicated", cache_id=cache_id)
                cache_ids.add(cache_id)
                cache_kind = cache_raw.get("kind", "warmup")
                if cache_kind not in ("warmup", "inference"):
                    fail(
                        "VALIDATION_ERROR",
                        "cache kind must be warmup or inference",
                        cache_id=cache_id,
                        kind=cache_kind,
                    )
                caches.append(
                    {
                        "cache_id": cache_id,
                        "replica_id": replica_id,
                        "kind": cache_kind,
                        "version": cache_version,
                        "shard_chain": cache_manifest.shard_chain(),
                        "shard_chain_digest": cache_manifest.directory_digest(),
                        "config_binding": catalog.config_binding(cache_manifest),
                        "valid": True,
                    }
                )
        return replicas, caches

    def _check_clock(
        self,
        clock: int,
        watermark: int,
        tolerance: int,
        *,
        operation: str,
    ) -> None:
        if watermark - clock > tolerance:
            fail(
                "CLOCK_ROLLBACK",
                "logical clock moved backwards beyond tolerance",
                operation=operation,
                logical_clock=clock,
                watermark=watermark,
                tolerance=tolerance,
                rollback_by=watermark - clock,
            )

    def _effective_default_operation_id(
        self, default_id: str, *, explicit: str | None
    ) -> str:
        if explicit:
            return default_id
        status = self.storage.operation_status(default_id)
        if status is None or status in ("running", "prepared"):
            return default_id
        if status == "committed":
            try:
                operation = self.storage.get_operation(default_id)
                current = self.storage.current_snapshot()
                result_snapshot = (operation.get("result") or {}).get("snapshot_id")
                if current is not None and result_snapshot == current.get("snapshot_id"):
                    return default_id
            except WorkbenchError:
                return default_id
        current = self.storage.current_snapshot()
        suffix = current["snapshot_id"][-8:] if current else "none"
        round_watermark = self.storage.get_meta("update_round", 0)
        return f"{default_id}-r{round_watermark}-{suffix}"

    def _write_snapshot_artifact(self, snapshot_id: str, payload: dict[str, Any]) -> Path:
        directory = self.storage.path.parent / "artifacts"
        path = directory / f"{snapshot_id}.json"
        if not path.exists():
            atomic_write(path, canonical_json_text(payload).encode("utf-8"))
        return path

    def _binding_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = snapshot.get("payload", {})
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            fail(
                "INVALID_STATE",
                "snapshot does not contain a manifest binding",
                snapshot_id=snapshot.get("snapshot_id"),
            )
        return {
            "version": manifest["version"],
            "snapshot_id": snapshot["snapshot_id"],
            "directory_digest": payload.get("directory_digest", snapshot.get("directory_digest")),
            "shard_chain": payload.get("shard_chain", []),
            "preprocess": manifest.get("preprocess"),
            "postprocess": manifest.get("postprocess"),
            "configs": payload.get("configs", {}),
            "config_binding": payload.get("config_binding"),
            "patch_chain": payload.get("patch_chain", []),
            "absolute_updated_ms": manifest.get("absolute_updated_ms"),
            "logical_clock": manifest.get("logical_clock"),
            "generation": manifest.get("generation"),
        }

    def _config_snapshot(self, catalog: Catalog, manifest: Manifest) -> dict[str, Any]:
        config_ids = set(
            catalog.config_closure(manifest.preprocess)
            + catalog.config_closure(manifest.postprocess)
        )
        return {
            config_id: catalog.registries.configs[config_id]
            for config_id in sorted(config_ids)
        }


def _path_or_none(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _validate_stages(stages: Iterable[int | str]) -> list[int]:
    if isinstance(stages, str):
        stages = [item for item in re.split(r"[\s,]+", stages.strip()) if item]
    result: list[int] = []
    for item in stages:
        if isinstance(item, str):
            try:
                value = int(item.strip())
            except ValueError:
                fail("GRAY_RATIO_INVALID", "gray ratio must be an integer", ratio=item)
        elif isinstance(item, int) and not isinstance(item, bool):
            value = item
        else:
            fail("GRAY_RATIO_INVALID", "gray ratio must be an integer", ratio=item)
        if value <= 0 or value > 100:
            fail("GRAY_RATIO_INVALID", "gray ratio must be in 1..100", ratio=value)
        result.append(value)
    if not result:
        fail("GRAY_RATIO_INVALID", "at least one gray stage is required")
    for earlier, later in zip(result, result[1:]):
        if later <= earlier:
            fail(
                "GRAY_RATIO_INVALID",
                "gray ratios must be strictly increasing",
                previous=earlier,
                current=later,
            )
    return result


def _rollback_ratios(release: dict[str, Any]) -> list[int]:
    stages = [int(value) for value in release.get("stages", [])]
    current_stage = int(release.get("current_stage", -1))
    current_ratio = int(release.get("current_ratio", 0))
    ratios: list[int] = []
    if current_ratio > 0:
        ratios.append(current_ratio)
    for index in range(min(current_stage, len(stages) - 1), -1, -1):
        value = stages[index]
        if value not in ratios:
            ratios.append(value)
    if 0 not in ratios:
        ratios.append(0)
    ratios = sorted(set(ratios), reverse=True)
    return ratios
