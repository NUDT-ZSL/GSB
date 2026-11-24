"""End-to-end orchestration for parsing, patch application and correlation."""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .checkpoint import CheckpointLoadResult, CheckpointManager
from .clustering import HostClusterResult
from .codes import Code
from .errors import EngineError
from .ledger import RuleLedger, build_catalog_from_ledger
from .models import (
    CHECKPOINT_STRUCTURE_VERSION,
    MERGE_SEMANTICS_VERSION,
    REPORT_SCHEMA_VERSION,
    BoundAlert,
    Cluster,
    ComputeState,
    EngineConfig,
    ParsedInputs,
    PatchBatch,
    RuleVersion,
)
from .parsing import load_inputs
from .rules import RuleCatalog, rule_version_as_dict
from .scheduler import HostJob, run_hosts
from .util import (
    atomic_write_json,
    digest_json,
    merge_key_for,
    stable_json,
)


@dataclass(frozen=True)
class InputPaths:
    alerts: Path
    v1: Path
    v2_shards: Path
    v2_meta: Path
    patches: Path

    @classmethod
    def from_directory(cls, directory: Path) -> "InputPaths":
        directory = Path(directory)
        return cls(
            alerts=directory / "alerts.jsonl",
            v1=directory / "rules_v1.json",
            v2_shards=directory / "v2_shards",
            v2_meta=directory / "v2_meta.sqlite",
            patches=directory / "patches.jsonl",
        )

    def load(self) -> ParsedInputs:
        return load_inputs(
            alerts_path=self.alerts,
            v1_path=self.v1,
            v2_shards_path=self.v2_shards,
            v2_meta_path=self.v2_meta,
            patches_path=self.patches,
        )


def _candidate_dict(version: RuleVersion) -> dict:
    return {
        "rule_key": version.rule_key,
        "candidate_rule_key": version.source_rule_key,
        "source_rule_key": version.source_rule_key,
        "source": version.source,
        "generation": version.generation,
        "effective_ms": version.effective_ms,
        "effective_error_ms": version.effective_error_ms,
    }


def _binding_for_alert(
    *,
    alert,
    catalog: RuleCatalog,
) -> tuple[dict, BoundAlert | None, dict | None]:
    resolution = catalog.resolve(alert.rule_key, alert.alert_ts_ms)
    base = {
        "alert_id": alert.alert_id,
        "host_id": alert.host_id,
        "rule_key": alert.rule_key,
        "alert_ts_ms": alert.alert_ts_ms,
        "version_margin_ms": resolution.margin_ms,
        "near_boundary": resolution.near_boundary,
        "cluster_id": None,
    }
    if resolution.status == "ambiguous":
        candidates = [_candidate_dict(item) for item in resolution.candidates]
        binding = {
            **base,
            "status": "version_ambiguous",
            "source": None,
            "generation": None,
            "effective_ms": None,
            "candidates": candidates,
        }
        diagnostic = {
            "code": Code.E_VERSION_AMBIGUOUS,
            "alert_id": alert.alert_id,
            "host_id": alert.host_id,
            "rule_key": alert.rule_key,
            "alert_ts_ms": alert.alert_ts_ms,
            "version_margin_ms": resolution.margin_ms,
            "candidates": candidates,
            "message": (
                "alert falls in the overlap of v1 relative-second and v2 "
                "absolute-millisecond effective intervals"
            ),
        }
        return binding, None, diagnostic
    if resolution.status == "not_effective" or resolution.version is None:
        return (
            {
                **base,
                "status": "not_effective",
                "source": None,
                "generation": None,
                "effective_ms": None,
            },
            None,
            None,
        )

    version = resolution.version
    binding = {
        **base,
        "status": "inactive" if version.tombstone or not version.enabled else "bound",
        "source": version.source,
        "generation": version.generation,
        "effective_ms": version.effective_ms,
        "effective_error_ms": version.effective_error_ms,
        "dedup_window_ms": version.dedup_window_ms,
        "window_error_ms": version.window_error_ms,
        "tombstone": version.tombstone,
        "enabled": version.enabled,
        "source_rule_key": version.source_rule_key,
    }
    if version.tombstone or not version.enabled:
        return binding, None, None
    bound = BoundAlert(
        alert=alert,
        version=version,
        version_margin_ms=resolution.margin_ms or 0,
        near_boundary=resolution.near_boundary,
        merge_key=merge_key_for(alert.rule_key, alert.payload_digest),
    )
    return binding, bound, None


def _resolve_bindings(
    alerts: Iterable,
    catalog: RuleCatalog,
) -> tuple[dict[str, dict], list[BoundAlert], list[dict]]:
    bindings: dict[str, dict] = {}
    bound_alerts: list[BoundAlert] = []
    ambiguities: list[dict] = []
    for alert in sorted(alerts, key=lambda item: item.alert_id):
        binding, bound, ambiguity = _binding_for_alert(
            alert=alert, catalog=catalog
        )
        bindings[alert.alert_id] = binding
        if bound is not None:
            bound_alerts.append(bound)
        if ambiguity is not None:
            ambiguities.append(ambiguity)
    return bindings, bound_alerts, ambiguities


def _jobs_for_hosts(
    *,
    all_alerts: Iterable,
    bound_alerts: Iterable[BoundAlert],
    include_cycles: bool,
    only_hosts: set[str] | None = None,
) -> list[HostJob]:
    all_by_host: dict[str, list] = {}
    for alert in all_alerts:
        all_by_host.setdefault(alert.host_id, []).append(alert)
    bound_by_host: dict[str, list[BoundAlert]] = {}
    for bound in bound_alerts:
        bound_by_host.setdefault(bound.alert.host_id, []).append(bound)

    hosts = set(all_by_host)
    if only_hosts is not None:
        hosts &= only_hosts
    jobs: list[HostJob] = []
    for host_id in sorted(hosts):
        host_alerts = sorted(
            all_by_host[host_id],
            key=lambda item: (item.alert_ts_ms, item.alert_id),
        )
        jobs.append(
            HostJob(
                host_id=host_id,
                all_alerts=tuple(host_alerts),
                bound_alerts=tuple(
                    sorted(
                        bound_by_host.get(host_id, []),
                        key=lambda item: (
                            item.alert.alert_ts_ms,
                            item.alert.alert_id,
                        ),
                    )
                ),
                total_alert_count=len(host_alerts),
                include_cycles=include_cycles,
            )
        )
    return jobs


def _merge_host_results(
    results: dict[str, HostClusterResult],
) -> tuple[dict[str, Cluster], dict[str, str], list[dict], list[dict]]:
    clusters: dict[str, Cluster] = {}
    alert_to_cluster: dict[str, str] = {}
    window_ambiguities: list[dict] = []
    proc_cycles: list[dict] = []
    for host_id in sorted(results):
        result = results[host_id]
        for cluster_id, cluster in sorted(result.clusters.items()):
            clusters[cluster_id] = cluster
            for alert_id, _ in cluster.alerts:
                alert_to_cluster[alert_id] = cluster_id
        window_ambiguities.extend(result.window_ambiguities)
        proc_cycles.extend(result.proc_cycles)
    return clusters, alert_to_cluster, window_ambiguities, proc_cycles


class Engine:
    def __init__(self, workspace: Path, config: EngineConfig | None = None):
        self.workspace = Path(workspace)
        self.config = config or EngineConfig()
        self.config.validate()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / "engine.sqlite3"
        self.checkpoint_dir = self.workspace / "checkpoints"
        self.temp_dir = self.workspace / "tmp"
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.workspace / ".engine.lock"
        self.report_path = self.workspace / "diagnostic_report.json"

    def run(
        self,
        inputs: InputPaths,
        *,
        resume: bool = False,
        write_report: bool = True,
    ) -> dict:
        run_started = time.perf_counter()
        parsed = inputs.load()
        all_host_ids = sorted({alert.host_id for alert in parsed.alerts})
        admission_capacity = (
            self.config.max_workers
            + self.config.pending_host_queue_limit
        )
        self._admitted_hosts = set(all_host_ids[:admission_capacity])
        input_fingerprint = digest_json(
            {
                "input_hashes": parsed.input_hashes,
                "checkpoint_structure_version": CHECKPOINT_STRUCTURE_VERSION,
                "merge_semantics_version": MERGE_SEMANTICS_VERSION,
                "engine_config": {
                    "max_workers": self.config.max_workers,
                    "pending_host_queue_limit": (
                        self.config.pending_host_queue_limit
                    ),
                    "per_host_timeout_ms": self.config.per_host_timeout_ms,
                    "max_alerts_per_host": self.config.max_alerts_per_host,
                },
            }
        )

        ledger = RuleLedger(self.db_path, lock_path=self.lock_path)
        checkpoint = CheckpointManager(
            db_path=self.db_path,
            checkpoint_dir=self.checkpoint_dir,
            temp_dir=self.temp_dir,
            lock_path=self.lock_path,
        )
        try:
            checkpoint_load = CheckpointLoadResult()
            restored = False
            state: ComputeState | None = None
            catalog: RuleCatalog | None = None
            applied_ids: set[str] = set()
            catalog_batches: list[PatchBatch] = []
            batch_audit: list[dict] = []
            checkpoint_info: dict = {}

            if resume:
                checkpoint_load = checkpoint.load_latest(
                    input_fingerprint=input_fingerprint
                )
                if checkpoint_load.found and not checkpoint_load.rebuild_required:
                    payload = checkpoint_load.payload or {}
                    ledger_ids = ledger.applied_batch_ids()
                    saved_ids = set(payload.get("applied_batch_ids", []))
                    if saved_ids.issubset(ledger_ids) and ledger.baseline_versions():
                        state = ComputeState.from_state_dict(payload["state"])
                        all_applied = ledger.applied_batches()
                        checkpoint_batches = [
                            item for item in all_applied if item.batch_id in saved_ids
                        ]
                        catalog = RuleCatalog(
                            v1_versions=[
                                item
                                for item in ledger.baseline_versions()
                                if item.source == "v1"
                            ],
                            v2_versions=[
                                item
                                for item in ledger.baseline_versions()
                                if item.source == "v2"
                            ],
                            patch_batches=checkpoint_batches,
                            observation_clock_ms=parsed.observation_clock_ms,
                        )
                        if catalog.digest != payload.get("rule_catalog_digest"):
                            checkpoint_load.rebuild_required = True
                            checkpoint_load.rebuild_reason = (
                                Code.E_CHECKPOINT_REBUILD
                            )
                            state = None
                            catalog = None
                        else:
                            extra = [
                                item
                                for item in all_applied
                                if item.batch_id not in saved_ids
                            ]
                            state, catalog, batch_audit = self._replay_extra_batches(
                                state=state,
                                catalog=catalog,
                                extra_batches=extra,
                                baseline_versions=ledger.baseline_versions(),
                                checkpoint_batches=checkpoint_batches,
                                alerts=parsed.alerts,
                            )
                            replayed_ids = {
                                item.get("batch_id") for item in batch_audit
                            }
                            restored_audit = [
                                {
                                    "batch_id": item.batch_id,
                                    "arrival_index": item.arrival_index,
                                    "logical_ts_ms": item.logical_ts_ms,
                                    "status": "restored_checkpoint",
                                    "affected_rule_keys": [],
                                    "affected_alert_ids": [],
                                    "affected_cluster_ids": [],
                                }
                                for item in sorted(
                                    checkpoint_batches,
                                    key=lambda value: value.arrival_index,
                                )
                                if item.batch_id not in replayed_ids
                            ]
                            batch_audit = restored_audit + batch_audit
                            applied_ids = ledger_ids
                            catalog_batches = all_applied
                            restored = True

            if not restored:
                checkpoint.clear()
                ledger.reset()
                baseline = parsed.v1_versions + parsed.v2_versions
                ledger.seed_baseline(baseline)
                ledger.set_meta(
                    "observation_clock_ms", parsed.observation_clock_ms
                )
                ledger.set_meta("input_fingerprint", input_fingerprint)
                catalog = RuleCatalog(
                    v1_versions=parsed.v1_versions,
                    v2_versions=parsed.v2_versions,
                    patch_batches=[],
                    observation_clock_ms=parsed.observation_clock_ms,
                )
                state, scheduler_report = self._compute_all_state(
                    parsed.alerts, catalog
                )
                batch_audit = []
                checkpoint_info = self._save_checkpoint(
                    checkpoint=checkpoint,
                    state=state,
                    catalog=catalog,
                    input_fingerprint=input_fingerprint,
                    applied_ids=[],
                    watermark=None,
                    logical_ts=0,
                )

            assert state is not None
            assert catalog is not None

            rejected_batch_ids = {
                item.get("details", {}).get("batch_id")
                for item in ledger.patch_rejection_records()
                if isinstance(item, dict)
            }
            for batch in sorted(
                parsed.patch_batches, key=lambda item: item.arrival_index
            ):
                if batch.batch_id in applied_ids:
                    continue
                if batch.batch_id in rejected_batch_ids:
                    continue
                old_catalog = catalog
                old_cluster_ids = set(state.clusters)
                try:
                    ledger.apply_patch_batch(batch)
                except EngineError as exc:
                    rejection = {
                        "source": "patch",
                        "record_index": batch.record_index,
                        "code": exc.code,
                        "message": exc.message,
                        "details": {
                            "batch_id": batch.batch_id,
                            **(exc.details or {}),
                        },
                    }
                    ledger.append_patch_rejection(rejection)
                    rejected_batch_ids.add(batch.batch_id)
                    batch_audit.append(
                        {
                            "batch_id": batch.batch_id,
                            "arrival_index": batch.arrival_index,
                            "logical_ts_ms": batch.logical_ts_ms,
                            "status": "rejected",
                            "code": exc.code,
                            "affected_rule_keys": [],
                            "affected_alert_ids": [],
                            "affected_cluster_ids": [],
                        }
                    )
                    continue

                applied_ids.add(batch.batch_id)
                catalog_batches = ledger.applied_batches()
                new_catalog = build_catalog_from_ledger(
                    ledger,
                    observation_clock_ms=parsed.observation_clock_ms,
                    batches=catalog_batches,
                )
                changed_keys = old_catalog.diff_keys(new_catalog)
                closure = new_catalog.dependency_closure(changed_keys)
                affected_alerts = sorted(
                    alert.alert_id
                    for alert in parsed.alerts
                    if alert.rule_key in closure
                )
                old_affected_clusters = sorted(
                    cluster_id
                    for cluster_id, cluster in state.clusters.items()
                    if cluster.rule_key in closure
                )
                state, _ = self._recompute_keys(
                    state=state,
                    catalog=new_catalog,
                    keys=closure,
                    alerts=parsed.alerts,
                )
                new_affected_clusters = sorted(
                    cluster_id
                    for cluster_id, cluster in state.clusters.items()
                    if cluster.rule_key in closure
                )
                batch_audit.append(
                    {
                        "batch_id": batch.batch_id,
                        "arrival_index": batch.arrival_index,
                        "logical_ts_ms": batch.logical_ts_ms,
                        "status": "applied",
                        "changed_rule_keys": sorted(changed_keys),
                        "affected_rule_keys": sorted(closure),
                        "affected_alert_ids": affected_alerts,
                        "affected_cluster_ids": sorted(
                            set(old_affected_clusters)
                            | set(new_affected_clusters)
                        ),
                        "removed_cluster_ids": old_affected_clusters,
                        "created_cluster_ids": new_affected_clusters,
                    }
                )
                catalog = new_catalog
                if self.config.checkpoint_every_batch:
                    checkpoint_info = self._save_checkpoint(
                        checkpoint=checkpoint,
                        state=state,
                        catalog=catalog,
                        input_fingerprint=input_fingerprint,
                        applied_ids=sorted(applied_ids),
                        watermark=ledger.applied_watermark_ms(),
                        logical_ts=batch.logical_ts_ms,
                    )

            final_catalog = build_catalog_from_ledger(
                ledger,
                observation_clock_ms=parsed.observation_clock_ms,
                batches=ledger.applied_batches(),
            )
            full_state, full_scheduler = self._compute_all_state(
                parsed.alerts, final_catalog
            )
            all_rejections = self._all_rejections(
                parsed.rejections,
                ledger.patch_rejection_records(),
            )
            recompute = self._verify_recompute(
                incremental_catalog=catalog,
                incremental_state=state,
                full_catalog=final_catalog,
                full_state=full_state,
                batch_audit=batch_audit,
                patch_rejections=ledger.patch_rejection_records(),
                rejections=all_rejections,
            )
            if not recompute["full_match"]:
                state = full_state
                catalog = final_catalog

            checkpoint_info = self._save_checkpoint(
                checkpoint=checkpoint,
                state=state,
                catalog=catalog,
                input_fingerprint=input_fingerprint,
                applied_ids=sorted(applied_ids),
                watermark=ledger.applied_watermark_ms(),
                logical_ts=ledger.applied_watermark_ms() or 0,
            )

            report = self._build_report(
                parsed=parsed,
                catalog=catalog,
                state=state,
                scheduler=full_scheduler,
                rejections=all_rejections,
                batch_audit=batch_audit,
                recompute=recompute,
                checkpoint_info=checkpoint_info,
                checkpoint_load=checkpoint_load,
                input_fingerprint=input_fingerprint,
                resume_requested=resume,
                restored=restored,
                elapsed_ms=(time.perf_counter() - run_started) * 1000.0,
            )
            if write_report:
                self.write_report(report)
            return report
        finally:
            checkpoint.close()
            ledger.close()

    def _replay_extra_batches(
        self,
        *,
        state: ComputeState,
        catalog: RuleCatalog,
        extra_batches: list[PatchBatch],
        baseline_versions: list[RuleVersion],
        checkpoint_batches: list[PatchBatch],
        alerts: list,
    ) -> tuple[ComputeState, RuleCatalog, list[dict]]:
        audit: list[dict] = []
        current_catalog = catalog
        current_state = state
        accepted_so_far = list(checkpoint_batches)
        for batch in sorted(
            extra_batches, key=lambda item: item.arrival_index
        ):
            accepted_so_far.append(batch)
            new_catalog = RuleCatalog(
                v1_versions=[
                    item
                    for item in baseline_versions
                    if item.source == "v1"
                ],
                v2_versions=[
                    item
                    for item in baseline_versions
                    if item.source == "v2"
                ],
                patch_batches=accepted_so_far,
                observation_clock_ms=current_catalog.observation_clock_ms,
            )
            changed = current_catalog.diff_keys(new_catalog)
            closure = new_catalog.dependency_closure(changed)
            old_cluster_ids = {
                cluster_id
                for cluster_id, cluster in current_state.clusters.items()
                if cluster.rule_key in closure
            }
            current_state, _ = self._recompute_keys(
                state=current_state,
                catalog=new_catalog,
                keys=closure,
                alerts=alerts,
            )
            new_cluster_ids = {
                cluster_id
                for cluster_id, cluster in current_state.clusters.items()
                if cluster.rule_key in closure
            }
            audit.append(
                {
                    "batch_id": batch.batch_id,
                    "arrival_index": batch.arrival_index,
                    "logical_ts_ms": batch.logical_ts_ms,
                    "status": "replayed_from_ledger",
                    "affected_rule_keys": sorted(closure),
                    "affected_alert_ids": sorted(
                        alert.alert_id
                        for alert in alerts
                        if alert.rule_key in closure
                    ),
                    "affected_cluster_ids": sorted(
                        old_cluster_ids | new_cluster_ids
                    ),
                }
            )
            current_catalog = new_catalog
        return current_state, current_catalog, audit

    def _compute_all_state(
        self,
        alerts: list,
        catalog: RuleCatalog,
    ) -> tuple[ComputeState, Any]:
        bindings, bound_alerts, ambiguities = _resolve_bindings(alerts, catalog)
        jobs = _jobs_for_hosts(
            all_alerts=alerts,
            bound_alerts=bound_alerts,
            include_cycles=True,
        )
        results, scheduler = run_hosts(jobs, self.config)
        clusters, alert_to_cluster, window_ambiguities, proc_cycles = (
            _merge_host_results(results)
        )
        for alert_id, binding in bindings.items():
            binding["cluster_id"] = alert_to_cluster.get(alert_id)
        state = ComputeState(
            bindings=bindings,
            clusters=clusters,
            version_ambiguities=sorted(
                ambiguities, key=lambda item: item["alert_id"]
            ),
            window_ambiguities=sorted(
                window_ambiguities,
                key=lambda item: (item["alert_id"], item["code"]),
            ),
            proc_cycles=sorted(
                proc_cycles,
                key=lambda item: (item["host_id"], item["code"]),
            ),
            scheduler_issues=list(scheduler.issues),
            host_elapsed_ms=dict(scheduler.host_elapsed_ms),
        )
        return state, scheduler

    def _recompute_keys(
        self,
        *,
        state: ComputeState,
        catalog: RuleCatalog,
        keys: set[str],
        alerts: list,
    ) -> tuple[ComputeState, set[str]]:
        affected_alerts = {
            alert.alert_id for alert in alerts if alert.rule_key in keys
        }
        for alert_id in affected_alerts:
            state.bindings.pop(alert_id, None)
        state.version_ambiguities = [
            item
            for item in state.version_ambiguities
            if item.get("alert_id") not in affected_alerts
        ]
        state.window_ambiguities = [
            item
            for item in state.window_ambiguities
            if item.get("alert_id") not in affected_alerts
        ]
        removed_clusters = {
            cluster_id
            for cluster_id, cluster in state.clusters.items()
            if cluster.rule_key in keys
        }
        for cluster_id in removed_clusters:
            state.clusters.pop(cluster_id, None)

        affected_alert_objects = [
            alert for alert in alerts if alert.alert_id in affected_alerts
        ]
        bindings, bound_alerts, ambiguities = _resolve_bindings(
            affected_alert_objects, catalog
        )
        state.bindings.update(bindings)
        state.version_ambiguities.extend(ambiguities)
        state.version_ambiguities.sort(key=lambda item: item["alert_id"])

        host_ids = {
            alert.host_id for alert in affected_alert_objects
        }
        jobs = _jobs_for_hosts(
            all_alerts=alerts,
            bound_alerts=bound_alerts,
            include_cycles=False,
            only_hosts=host_ids & self._admitted_hosts,
        )
        results, scheduler = run_hosts(jobs, self.config)
        (
            new_clusters,
            alert_to_cluster,
            window_ambiguities,
            _cycles,
        ) = _merge_host_results(results)
        state.clusters.update(new_clusters)
        state.window_ambiguities.extend(window_ambiguities)
        state.window_ambiguities.sort(
            key=lambda item: (item["alert_id"], item["code"])
        )
        for alert_id in affected_alerts:
            if alert_id in state.bindings:
                state.bindings[alert_id]["cluster_id"] = alert_to_cluster.get(
                    alert_id
                )

        known_issues = {
            (item.get("host_id"), item.get("code"), stable_json(item))
            for item in state.scheduler_issues
        }
        for issue in scheduler.issues:
            signature = (
                issue.get("host_id"),
                issue.get("code"),
                stable_json(issue),
            )
            if signature not in known_issues:
                state.scheduler_issues.append(issue)
                known_issues.add(signature)
        state.scheduler_issues.sort(
            key=lambda item: (
                item.get("host_id", ""),
                item.get("code", ""),
            )
        )
        return state, affected_alerts

    def _save_checkpoint(
        self,
        *,
        checkpoint: CheckpointManager,
        state: ComputeState,
        catalog: RuleCatalog,
        input_fingerprint: str,
        applied_ids: Iterable[str],
        watermark: int | None,
        logical_ts: int,
    ) -> dict:
        return checkpoint.save(
            state=state.to_state_dict(),
            input_fingerprint=input_fingerprint,
            applied_batch_ids=list(applied_ids),
            applied_watermark_ms=watermark,
            logical_ts_ms=logical_ts,
            rule_catalog_digest=catalog.digest,
        )

    def _verify_recompute(
        self,
        *,
        incremental_catalog: RuleCatalog,
        incremental_state: ComputeState,
        full_catalog: RuleCatalog,
        full_state: ComputeState,
        batch_audit: list[dict],
        patch_rejections: list[dict],
        rejections: list[dict],
    ) -> dict:
        left = self._comparison_projection(
            incremental_catalog, incremental_state, rejections
        )
        right = self._comparison_projection(
            full_catalog, full_state, rejections
        )
        mismatches = self._diff_values(left, right)
        match = not mismatches
        recompute = {
            "full_match": match,
            "batches": batch_audit,
            "patch_rejections": patch_rejections,
            "incremental_projection_digest": digest_json(left),
            "full_projection_digest": digest_json(right),
            "mismatches": mismatches,
            "code": None if match else Code.E_RECOMPUTE_MISMATCH,
        }
        if mismatches:
            recompute["message"] = (
                "incremental recomputation differs from a clean full rebuild"
            )
        return recompute

    def _comparison_projection(
        self,
        catalog: RuleCatalog,
        state: ComputeState,
        rejections: list[dict],
    ) -> dict:
        state_dict = state.to_state_dict()
        return {
            "rules": catalog.to_report(),
            "bindings": state_dict["bindings"],
            "clusters": state_dict["clusters"],
            "version_ambiguities": state_dict["version_ambiguities"],
            "window_ambiguities": state_dict["window_ambiguities"],
            "proc_cycles": state_dict["proc_cycles"],
            "rejections": rejections,
        }

    def _diff_values(
        self,
        left: Any,
        right: Any,
        path: str = "$",
        limit: int = 20,
    ) -> list[dict]:
        mismatches: list[dict] = []

        def walk(a: Any, b: Any, current_path: str) -> None:
            if len(mismatches) >= limit:
                return
            if type(a) is not type(b):
                mismatches.append(
                    {
                        "path": current_path,
                        "left_type": type(a).__name__,
                        "right_type": type(b).__name__,
                    }
                )
                return
            if isinstance(a, dict):
                for key in sorted(set(a) | set(b)):
                    if key not in a:
                        mismatches.append(
                            {"path": f"{current_path}.{key}", "left": "<missing>"}
                        )
                    elif key not in b:
                        mismatches.append(
                            {"path": f"{current_path}.{key}", "right": "<missing>"}
                        )
                    else:
                        walk(a[key], b[key], f"{current_path}.{key}")
                    if len(mismatches) >= limit:
                        return
                return
            if isinstance(a, list):
                if len(a) != len(b):
                    mismatches.append(
                        {
                            "path": current_path,
                            "left_length": len(a),
                            "right_length": len(b),
                        }
                    )
                    return
                for index, (left_item, right_item) in enumerate(zip(a, b)):
                    walk(left_item, right_item, f"{current_path}[{index}]")
                    if len(mismatches) >= limit:
                        return
                return
            if a != b:
                mismatches.append(
                    {"path": current_path, "left": a, "right": b}
                )

        walk(left, right, path)
        return mismatches

    def _all_rejections(
        self,
        parse_rejections: list[dict],
        patch_rejections: list[dict],
    ) -> list[dict]:
        combined = list(parse_rejections) + list(patch_rejections)
        combined.sort(
            key=lambda item: (
                item.get("source", ""),
                item.get("record_index", 0),
                item.get("code", ""),
                stable_json(item.get("details", {})),
            )
        )
        return combined

    def _build_report(
        self,
        *,
        parsed: ParsedInputs,
        catalog: RuleCatalog,
        state: ComputeState,
        scheduler,
        rejections: list[dict],
        batch_audit: list[dict],
        recompute: dict,
        checkpoint_info: dict,
        checkpoint_load: CheckpointLoadResult,
        input_fingerprint: str,
        resume_requested: bool,
        restored: bool,
        elapsed_ms: float,
    ) -> dict:
        clusters = state.cluster_list()
        bindings = state.sorted_bindings()
        version_ambiguities = sorted(
            state.version_ambiguities, key=lambda item: item["alert_id"]
        )
        window_ambiguities = sorted(
            state.window_ambiguities,
            key=lambda item: (item["alert_id"], item["code"]),
        )
        proc_cycles = sorted(
            state.proc_cycles,
            key=lambda item: (item["host_id"], str(item.get("cycle"))),
        )
        code_counts: dict[str, int] = {}
        for rejection in rejections:
            code = rejection.get("code", Code.E_INTERNAL)
            code_counts[code] = code_counts.get(code, 0) + 1
        for conflict in catalog.conflicts:
            code = conflict["code"]
            code_counts[code] = code_counts.get(code, 0) + 1
        for diagnostic in version_ambiguities:
            code_counts[Code.E_VERSION_AMBIGUOUS] = (
                code_counts.get(Code.E_VERSION_AMBIGUOUS, 0) + 1
            )
        for diagnostic in window_ambiguities:
            code_counts[Code.E_WINDOW_AMBIGUOUS] = (
                code_counts.get(Code.E_WINDOW_AMBIGUOUS, 0) + 1
            )
        for diagnostic in proc_cycles:
            code_counts[Code.E_PROC_CYCLE] = (
                code_counts.get(Code.E_PROC_CYCLE, 0) + 1
            )
        for issue in scheduler.issues:
            code = issue.get("code", Code.E_INTERNAL)
            code_counts[code] = code_counts.get(code, 0) + 1
        for binding in bindings:
            if binding["status"] == "not_effective":
                code_counts[Code.E_RULE_NOT_EFFECTIVE] = (
                    code_counts.get(Code.E_RULE_NOT_EFFECTIVE, 0) + 1
                )
            elif binding["status"] == "inactive":
                code_counts[Code.E_RULE_INACTIVE] = (
                    code_counts.get(Code.E_RULE_INACTIVE, 0) + 1
                )
        if not recompute["full_match"]:
            code_counts[Code.E_RECOMPUTE_MISMATCH] = (
                code_counts.get(Code.E_RECOMPUTE_MISMATCH, 0) + 1
            )
        code_counts = dict(sorted(code_counts.items()))

        logical_order = [
            item.logical_ts_ms
            for item in sorted(
                parsed.patch_batches, key=lambda value: value.arrival_index
            )
            if item.batch_id
            in {
                audit.get("batch_id")
                for audit in batch_audit
                if audit.get("status")
                in ("applied", "replayed_from_ledger", "restored_checkpoint")
            }
        ]
        out_of_order = any(
            logical_order[index] > logical_order[index + 1]
            for index in range(max(0, len(logical_order) - 1))
        )
        applied_batch_ids = {
            audit.get("batch_id")
            for audit in batch_audit
            if audit.get("status")
            in ("applied", "replayed_from_ledger", "restored_checkpoint")
        }
        accepted_alerts = len(parsed.alerts)
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "checkpoint_structure_version": CHECKPOINT_STRUCTURE_VERSION,
            "merge_semantics_version": MERGE_SEMANTICS_VERSION,
            "status": "OK" if recompute["full_match"] else "MISMATCH",
            "input_fingerprint": input_fingerprint,
            "time_basis": {
                "observation_clock_ms": parsed.observation_clock_ms,
                "v1_relative_unit": "second_floor",
                "v1_effective_error_ms": 999,
                "v2_unit": "absolute_millisecond",
                "system_clock_used_for_rule_time": False,
            },
            "counts": {
                "alerts_accepted": accepted_alerts,
                "alerts_rejected": sum(
                    1 for item in rejections if item.get("source") == "alert"
                ),
                "alerts_bound": sum(
                    1 for item in bindings if item["status"] == "bound"
                ),
                "alerts_version_ambiguous": len(version_ambiguities),
                "alerts_unclustered": sum(
                    1 for item in bindings if item.get("cluster_id") is None
                ),
                "clusters": len(clusters),
                "rule_versions": len(catalog.versions),
                "rule_conflicts": len(catalog.conflicts),
                "window_ambiguities": len(window_ambiguities),
                "proc_cycles": len(proc_cycles),
                "rejections": len(rejections),
                "patch_batches_arrived": len(parsed.patch_batches),
                "patch_batches_applied": len(applied_batch_ids),
                "patch_batches_rejected": len(
                    {
                        item["details"]["batch_id"]
                        for item in rejections
                        if item.get("source") == "patch"
                        and "batch_id" in item.get("details", {})
                    }
                ),
            },
            "business_codes": code_counts,
            "rejections": rejections,
            "rules": catalog.to_report(),
            "bindings": bindings,
            "clusters": clusters,
            "diagnostics": {
                "version_ambiguous": version_ambiguities,
                "window_ambiguous": window_ambiguities,
                "proc_cycles": proc_cycles,
                "recompute": recompute,
            },
            "patch_processing": {
                "arrival_order": [
                    item.batch_id
                    for item in sorted(
                        parsed.patch_batches,
                        key=lambda value: value.arrival_index,
                    )
                ],
                "logical_order": [
                    item.batch_id
                    for item in sorted(
                        parsed.patch_batches,
                        key=lambda value: (
                            value.logical_ts_ms,
                            value.batch_id,
                        ),
                    )
                ],
                "out_of_order_arrival": out_of_order,
                "batches": batch_audit,
            },
            "scheduler": {
                "config": {
                    "max_workers": self.config.max_workers,
                    "pending_host_queue_limit": (
                        self.config.pending_host_queue_limit
                    ),
                    "per_host_timeout_ms": self.config.per_host_timeout_ms,
                    "max_alerts_per_host": self.config.max_alerts_per_host,
                },
                "hosts": scheduler.hosts,
                "processed_hosts": scheduler.processed_hosts,
                "rejected_hosts": scheduler.rejected_hosts,
                "elapsed_ms": round(scheduler.elapsed_ms, 6),
                "host_elapsed_ms": dict(
                    sorted(scheduler.host_elapsed_ms.items())
                ),
                "issues": list(scheduler.issues),
            },
            "checkpoint": {
                "resume_requested": resume_requested,
                "restored": restored,
                "latest_saved": checkpoint_info,
                "load_rebuild_required": checkpoint_load.rebuild_required,
                "load_rebuild_reason": checkpoint_load.rebuild_reason,
                "load_residuals": checkpoint_load.residuals,
                "orphan_temp_files": checkpoint_load.orphan_temp_files,
            },
            "input_hashes": parsed.input_hashes,
            "runtime": {
                "python_version": platform.python_version(),
                "implementation": platform.python_implementation(),
                "elapsed_ms": round(elapsed_ms, 6),
            },
        }

    def write_report(self, report: dict) -> None:
        atomic_write_json(
            self.report_path,
            report,
            lock_path=self.lock_path,
            validator=_validate_report,
        )


def _validate_report(report: Any) -> None:
    if not isinstance(report, dict):
        raise ValueError("diagnostic report must be an object")
    for field in (
        "schema_version",
        "status",
        "counts",
        "rejections",
        "rules",
        "clusters",
        "diagnostics",
    ):
        if field not in report:
            raise ValueError(f"diagnostic report is missing {field}")
    if not isinstance(report["clusters"], list):
        raise ValueError("diagnostic report clusters must be a list")
