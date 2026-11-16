"""End-to-end deterministic panoptic pipeline orchestrator."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .converters import (
    ConversionLoss,
    load_metadata,
    parse_v1_to_ir,
    parse_v2_to_ir,
)
from .errors import E_HARD_CRASH_SIMULATED, PipelineError
from .fingerprints import config_hash, fusion_dependency_key, instance_cache_key
from .fusion import FusionResult, fuse_scene
from .models import FailureRecord, InstanceRecord, PipelineConfig, STAGE_FUSE, STAGE_PARSE_V1, STAGE_PARSE_V2
from .scheduler import Task, run_tasks
from .snapshot import SnapshotStore, atomic_write_json

HARD_CRASH_CODE = "E_HARD_CRASH_SIMULATED"


@dataclass
class RunStats:
    total_instances: int = 0
    reused_instances: int = 0
    invalidated_instances: int = 0
    decoded_instances: int = 0
    reused_fusions: int = 0
    recomputed_fusions: int = 0
    snapshot_version: int | None = None


@dataclass
class PipelineRunResult:
    instances: dict[str, InstanceRecord]
    fusion_results: dict[str, FusionResult]
    failures: list[FailureRecord]
    conversion_losses: list[ConversionLoss]
    stats: RunStats
    manifest: dict[str, Any]
    signaled: bool = False


class PanopticPipeline:
    def __init__(
        self,
        sample_dir: str | Path,
        config: PipelineConfig,
        work_dir: str | Path,
        *,
        file_overrides: dict[str, Path] | None = None,
    ) -> None:
        self.sample_dir = Path(sample_dir)
        self.config = config
        self.work_dir = Path(work_dir)
        self.store = SnapshotStore(self.work_dir / "snapshots")
        self.file_overrides = file_overrides or {}
        self.metadata = load_metadata(self.sample_dir / "metadata.json")
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self.signaled = False
        self.stats = RunStats()
        self.failures: list[FailureRecord] = []
        self.losses: list[ConversionLoss] = []
        self.current_config_hash = config_hash(_hashable_config(config))

    def request_shutdown(self) -> None:
        with self._lock:
            self.signaled = True
        self._shutdown.set()

    # ------------------------------------------------------------------ run
    def run(self) -> PipelineRunResult:
        recovered = self._recover_if_compatible()
        prior_instances: dict[str, InstanceRecord] = {}
        prior_fusion: dict[str, FusionResult] = {}
        prior_manifest: dict[str, Any] = {}
        next_version = 1
        if recovered is not None:
            prior_manifest, prior_instances, prior_fusion = recovered
            next_version = int(prior_manifest["version_number"]) + 1

        scene_files = self._discover_scenes()
        instances: dict[str, InstanceRecord] = dict(prior_instances)
        fusion_results: dict[str, FusionResult] = dict(prior_fusion)
        invalidated_ids: set[str] = set()
        processed_scenes = 0
        version = next_version - 1
        last_committed_version: int | None = None

        for scene_id, paths in sorted(scene_files.items()):
            if self._shutdown.is_set():
                break
            v1_path = self.file_overrides.get(paths["v1"].name, paths["v1"])
            v2_path = self.file_overrides.get(paths["v2"].name, paths["v2"])
            scene_tasks = [
                Task(
                    instance_id=f"{scene_id}#v1",
                    stage=STAGE_PARSE_V1,
                    func=lambda p=v1_path, s=scene_id: parse_v1_to_ir(p, s, self.metadata, self.config),
                ),
                Task(
                    instance_id=f"{scene_id}#v2",
                    stage=STAGE_PARSE_V2,
                    func=lambda p=v2_path, s=scene_id: parse_v2_to_ir(p, s, self.config),
                ),
            ]
            batch = run_tasks(scene_tasks, self.config.workers)
            self.failures.extend(batch.failures)
            keyed_fresh: dict[str, InstanceRecord] = {}
            for _instance_id, _stage, records in batch.successes:
                for record in records:
                    cache_key = record.cache_key or instance_cache_key(record)
                    keyed_fresh[record.instance_id] = (
                        record if record.cache_key else record.with_cache_key(cache_key)
                    )

            scene_instances, scene_invalidated = self._reuse_or_replace(
                {key: value for key, value in instances.items() if value.scene_id == scene_id},
                keyed_fresh,
            )
            instances.update(scene_instances)
            invalidated_ids.update(scene_invalidated)
            self._maybe_hard_crash_after_parse(scene_invalidated)

            scene_fusion, scene_fusion_manifest = self._fuse_all(
                scene_instances,
                {scene_id: fusion_results.get(scene_id)} if fusion_results.get(scene_id) else {},
                scene_invalidated,
            )
            fusion_results.update(scene_fusion)
            processed_scenes += 1

            if processed_scenes % max(1, self.config.snapshot_commit_interval) == 0:
                version += 1
                fusion_manifest = self._manifest_fusion(instances, fusion_results)
                manifest = self._build_manifest(version, instances, fusion_manifest)
                self.store.commit(version, manifest, instances, fusion_results)
                last_committed_version = version
                self.stats.snapshot_version = version

        self.stats.total_instances = len(instances)
        self.stats.invalidated_instances = len(invalidated_ids)
        self.stats.reused_instances = sum(
            1
            for instance_id, record in instances.items()
            if instance_id in prior_instances
            and prior_instances[instance_id].cache_key == record.cache_key
        )
        self.stats.decoded_instances = self.stats.total_instances - self.stats.reused_instances

        interval = max(1, self.config.snapshot_commit_interval)
        if last_committed_version is None or processed_scenes % interval != 0:
            version += 1
            manifest = self._build_manifest(
                version, instances, self._manifest_fusion(instances, fusion_results)
            )
            self.store.commit(version, manifest, instances, fusion_results)
            last_committed_version = version
            self.stats.snapshot_version = version
        return PipelineRunResult(
            instances=instances,
            fusion_results=fusion_results,
            failures=sorted(self.failures, key=lambda item: item.sort_key),
            conversion_losses=sorted(self.losses, key=lambda item: (item.instance_id, item.field_name)),
            stats=self.stats,
            manifest=manifest,
            signaled=self.signaled,
        )

    # -------------------------------------------------------------- helpers
    def _discover_scenes(self) -> dict[str, dict[str, Path]]:
        scenes: dict[str, dict[str, Path]] = {}
        for path in sorted(self.sample_dir.glob("*.v1.bin")):
            scene_id = path.name[: -len(".v1.bin")]
            v2_path = self.sample_dir / f"{scene_id}.v2.json"
            if not v2_path.exists():
                raise PipelineError("E_INPUT_INVALID", f"missing v2 package for scene {scene_id}")
            scenes[scene_id] = {"v1": path, "v2": v2_path}
        return scenes

    def _recover_if_compatible(self):
        try:
            recovered = self.store.recover()
        except PipelineError:
            return None
        if recovered is None:
            return None
        manifest, _instances, _fusion = recovered
        if manifest.get("config_hash") != self.current_config_hash:
            return None
        return recovered

    def _reuse_or_replace(
        self,
        prior: dict[str, InstanceRecord],
        fresh: dict[str, InstanceRecord],
    ) -> tuple[dict[str, InstanceRecord], set[str]]:
        merged: dict[str, InstanceRecord] = {}
        invalidated: set[str] = set()
        all_ids = sorted(set(prior) | set(fresh))
        for instance_id in all_ids:
            new_record = fresh.get(instance_id)
            old_record = prior.get(instance_id)
            if new_record is None:
                if old_record is not None:
                    merged[instance_id] = old_record
                continue
            if old_record is not None and old_record.cache_key == new_record.cache_key:
                merged[instance_id] = old_record
            else:
                merged[instance_id] = new_record
                invalidated.add(instance_id)
        return merged, invalidated

    def _fuse_all(
        self,
        instances: dict[str, InstanceRecord],
        prior_fusion: dict[str, FusionResult],
        invalidated_ids: set[str],
    ) -> tuple[dict[str, FusionResult], dict[str, dict[str, Any]]]:
        by_scene: dict[str, list[InstanceRecord]] = {}
        for record in sorted(instances.values(), key=lambda item: item.instance_id):
            by_scene.setdefault(record.scene_id, []).append(record)

        fusion_results: dict[str, FusionResult] = {}
        manifest_entries: dict[str, dict[str, Any]] = {}
        for scene_id, records in sorted(by_scene.items()):
            participant_keys = sorted(record.cache_key for record in records if record.cache_key)
            dependency_key = fusion_dependency_key(
                scene_id,
                participant_keys,
                self.config.iou_threshold,
                self.config.retention_policy,
                self.config.min_coverage,
            )
            prior = prior_fusion.get(scene_id)
            affected = any(record.instance_id in invalidated_ids for record in records)
            if prior is not None and not affected:
                fusion_results[scene_id] = prior
                self.stats.reused_fusions += 1
            else:
                task = Task(
                    instance_id=scene_id,
                    stage=STAGE_FUSE,
                    func=lambda r=records: fuse_scene(
                        scene_id,
                        r,
                        self.config.iou_threshold,
                        self.config.min_coverage,
                        self.config.retention_policy,
                        self.config.boundary_iou_threshold,
                    ),
                )
                batch = run_tasks([task], self.config.workers)
                self.failures.extend(batch.failures)
                if batch.successes:
                    fusion_results[scene_id] = batch.successes[0][2]
                    self.stats.recomputed_fusions += 1
            manifest_entries[scene_id] = {
                "dependency_key": dependency_key,
                "participants": sorted(record.instance_id for record in records),
                "reused": scene_id in prior_fusion and not affected,
            }
        return fusion_results, manifest_entries

    def _manifest_fusion(
        self,
        instances: dict[str, InstanceRecord],
        fusion_results: dict[str, FusionResult],
    ) -> dict[str, dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        by_scene: dict[str, list[InstanceRecord]] = {}
        for record in instances.values():
            by_scene.setdefault(record.scene_id, []).append(record)
        for scene_id, records in sorted(by_scene.items()):
            participant_keys = sorted(record.cache_key for record in records if record.cache_key)
            entries[scene_id] = {
                "dependency_key": fusion_dependency_key(
                    scene_id,
                    participant_keys,
                    self.config.iou_threshold,
                    self.config.retention_policy,
                    self.config.min_coverage,
                ),
                "participants": sorted(record.instance_id for record in records),
                "reused": scene_id in fusion_results,
            }
        return entries

    def _build_manifest(
        self,
        version: int,
        instances: dict[str, InstanceRecord],
        fusion_manifest: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        instance_versions = {
            instance_id: record.cache_key for instance_id, record in sorted(instances.items())
        }
        return {
            "global_version": f"panseg-{version:06d}",
            "version_number": version,
            "format_versions": {"v1": 1, "v2": 2},
            "config_hash": self.current_config_hash,
            "retention_policy": self.config.retention_policy,
            "iou_threshold": self.config.iou_threshold,
            "instance_versions": instance_versions,
            "fusion_versions": fusion_manifest,
        }

    def _maybe_hard_crash_after_parse(self, invalidated_ids: set[str]) -> None:
        rule = self.config.fault_injection.get("snapshot_hard_crash")
        if not rule:
            return
        marker = rule.get("after_instance")
        if marker in invalidated_ids:
            raise PipelineError(
                E_HARD_CRASH_SIMULATED,
                f"simulated hard crash after processing {marker}; no snapshot committed",
                instance_id=marker,
                stage="snapshot",
            )


def _hashable_config(config: PipelineConfig) -> dict[str, Any]:
    payload = config.to_dict()
    # Fault injection changes failure behavior but not deterministic data
    # outputs; keep cache identity independent of injected faults.
    payload.pop("fault_injection", None)
    return payload


def run_conversion_roundtrip(
    sample_dir: Path,
    work_dir: Path,
    config: PipelineConfig,
) -> tuple[list[ConversionLoss], list[ConversionLoss], dict[str, str]]:
    """Exercise both conversion directions and collect every dropped field."""

    from .converters import downgrade_v2_to_v1, upgrade_v1_to_v2

    metadata = load_metadata(sample_dir / "metadata.json")
    conversion_dir = work_dir / "converted"
    conversion_dir.mkdir(parents=True, exist_ok=True)
    upgrade_losses: list[ConversionLoss] = []
    downgrade_losses: list[ConversionLoss] = []
    artifacts: dict[str, str] = {}

    for v1_path in sorted(sample_dir.glob("*.v1.bin")):
        scene_id = v1_path.name[: -len(".v1.bin")]
        v1_records = parse_v1_to_ir(v1_path, scene_id, metadata, config.without_faults())
        upgraded_path = conversion_dir / f"{scene_id}.upgraded.v2.json"
        upgrade_losses.extend(upgrade_v1_to_v2(v1_records, upgraded_path, config.without_faults()))
        artifacts[upgraded_path.name] = str(upgraded_path)

    for v2_path in sorted(sample_dir.glob("*.v2.json")):
        scene_id = v2_path.name[: -len(".v2.json")]
        v2_records = parse_v2_to_ir(v2_path, scene_id, config.without_faults())
        downgraded_path = conversion_dir / f"{scene_id}.downgraded.v1.bin"
        downgrade_losses.extend(
            downgrade_v2_to_v1(v2_records, downgraded_path, config.without_faults())
        )
        artifacts[downgraded_path.name] = str(downgraded_path)

    return upgrade_losses, downgrade_losses, artifacts
