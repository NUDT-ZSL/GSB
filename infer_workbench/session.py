"""Whole-version inference sessions, snapshots, restoration, and replay checks."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .assets import Assets
from .batching import BatchPlan, plan_batches
from .canonical import atomic_write_json, bits_equal, float32_bits, relative_path
from .config import BatchConfig
from .engine import BOUND_METHOD, FixedLayer, prepare_fixed_model, run_batch
from .errors import WorkbenchError
from .snapshots import (
    SNAPSHOT_SCHEMA,
    load_snapshot,
    preserve_readonly_backup,
    save_snapshot,
    seal_snapshot,
)
from .versioning import VersionArchive, compare_versions


REPORT_SCHEMA = "infer-workbench.report.v1"


def _validate_stop_after(stop_after: int | None) -> None:
    if stop_after is None:
        return
    if isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 0:
        raise WorkbenchError(
            "E_CONFIG", "stop_after 必须为非负整数", {"field": "stop_after", "value": stop_after}
        )


class InferenceSession:
    """Holds exactly one active configuration version at a time."""

    def __init__(self, assets: Assets, *, snapshot_dir: str | Path = "snapshots"):
        self.assets = assets
        self.snapshot_dir = Path(snapshot_dir)
        self.config: BatchConfig | None = None
        self.plans: list[BatchPlan] = []
        self.batches: list[dict[str, Any]] = []
        self.status = "idle"
        self.last_restore: dict[str, Any] | None = None
        self.last_replay: dict[str, Any] | None = None
        self._fixed_cache: dict[int, tuple[FixedLayer, ...]] = {}
        self._archives: dict[str, VersionArchive] = {}
        self._stale_versions: list[dict[str, Any]] = []
        self._transition_old: VersionArchive | None = None
        self._transition: dict[str, Any] | None = None

    @property
    def config_version(self) -> str | None:
        return self.config.config_version if self.config else None

    @property
    def next_batch_index(self) -> int:
        return len(self.batches) + 1

    @property
    def complete(self) -> bool:
        return bool(self.plans) and len(self.batches) == len(self.plans)

    def _fixed_layers(self) -> tuple[FixedLayer, ...] | None:
        if self.config is None or self.config.precision_mode != "fixed":
            return None
        scale = self.config.quant_scale
        if scale not in self._fixed_cache:
            self._fixed_cache[scale] = prepare_fixed_model(self.assets, self.config)
        return self._fixed_cache[scale]

    def _current_archive(self, *, stale: bool) -> VersionArchive | None:
        if self.config is None or not self.plans:
            return None
        archived_batches = []
        for batch in self.batches:
            item = copy.deepcopy(batch)
            if stale:
                item["stale"] = True
            archived_batches.append(item)
        return VersionArchive(
            config=self.config,
            data_digest=self.assets.data_digest,
            parameter_digest=self.assets.parameter_digest,
            plans=tuple(self.plans),
            batches=tuple(archived_batches),
            complete=self.complete,
        )

    def _clear_active(
        self,
        *,
        reason: str,
        record_stale: bool,
    ) -> None:
        archive = self._current_archive(stale=record_stale)
        if archive is not None:
            self._archives[archive.config_version] = archive
            if record_stale:
                self._stale_versions.append(
                    {
                        "config_version": archive.config_version,
                        "completed_batches": len(archive.batches),
                        "total_batches": len(archive.plans),
                        "reason": reason,
                        "stale": True,
                    }
                )
        self.batches.clear()
        self.plans.clear()
        self.config = None
        self.status = "idle"

    def start(self, config: BatchConfig) -> None:
        if not isinstance(config, BatchConfig):
            raise WorkbenchError("E_CONFIG", "start 需要 BatchConfig 实例")

        new_plans = plan_batches(self.assets, config)
        old_archive = self._current_archive(stale=True)
        old_version = self.config_version
        version_changed = old_version is not None and old_version != config.config_version
        self._clear_active(
            reason=f"replaced_by:{config.config_version}", record_stale=version_changed
        )
        self._transition_old = old_archive if version_changed else None
        self._transition = None
        self.last_restore = None
        self.last_replay = None
        self.config = config
        self.plans = new_plans
        self.status = "running"

    def run_batch(self) -> dict[str, Any]:
        if self.config is None:
            raise WorkbenchError("E_CONFIG", "会话尚未加载批处理配置")
        if self.next_batch_index > len(self.plans):
            raise WorkbenchError(
                "E_CONFIG",
                "所有批次均已完成，不能继续追加批次",
                {"completed": len(self.batches), "total": len(self.plans)},
            )
        plan = self.plans[len(self.batches)]
        batch = run_batch(self.assets, self.config, plan, self._fixed_layers())
        self.batches.append(batch)
        self.status = "complete" if self.complete else "running"
        return batch

    def _write_current_snapshot(self, path: str | Path) -> Path:
        return save_snapshot(path, self.snapshot_payload())

    def run(
        self,
        config: BatchConfig,
        *,
        stop_after: int | None = None,
        snapshot_path: str | Path | None = None,
    ) -> dict[str, Any]:
        _validate_stop_after(stop_after)
        self.start(config)
        self._execute(stop_after=stop_after, snapshot_path=snapshot_path)
        return self.build_report()

    def continue_remaining(
        self,
        *,
        stop_after: int | None = None,
        snapshot_path: str | Path | None = None,
    ) -> dict[str, Any]:
        _validate_stop_after(stop_after)
        if self.config is None:
            raise WorkbenchError("E_CONFIG", "没有可继续的配置版本")
        if self.status == "incompatible":
            raise WorkbenchError("E_DATA", "当前恢复状态与本地数据不兼容，无法继续批次")
        if self.complete:
            return self.build_report()
        self.status = "running"
        self._execute(stop_after=stop_after, snapshot_path=snapshot_path)
        return self.build_report()

    def _execute(
        self,
        *,
        stop_after: int | None,
        snapshot_path: str | Path | None,
    ) -> None:
        executed = 0
        while not self.complete and (stop_after is None or executed < stop_after):
            self.run_batch()
            executed += 1
            if snapshot_path is not None:
                self._write_current_snapshot(snapshot_path)
        if not self.complete and stop_after is not None and executed >= stop_after:
            self.status = "interrupted"

    def snapshot_payload(self) -> dict[str, Any]:
        if self.config is None:
            raise WorkbenchError("E_CONFIG", "没有可持久化的配置版本")
        return {
            "schema": SNAPSHOT_SCHEMA,
            "snapshot_id": f"{self.config.config_version}-{len(self.batches):02d}",
            "data_digest": self.assets.data_digest,
            "parameter_digest": self.assets.parameter_digest,
            "config": self.config.to_dict(),
            "config_version": self.config.config_version,
            "batch_plan": [plan.to_dict() for plan in self.plans],
            "completed_batches": copy.deepcopy(self.batches),
            "next_batch_index": self.next_batch_index,
            "complete": self.complete,
            "status": self.status,
        }

    def save_snapshot(self, path: str | Path) -> Path:
        return self._write_current_snapshot(path)

    def restore_snapshot(
        self,
        path: str | Path,
        *,
        backup_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        snapshot = load_snapshot(path)
        backup = preserve_readonly_backup(
            path, backup_dir if backup_dir is not None else self.snapshot_dir / "backups"
        )
        snapshot_config = BatchConfig.from_dict(snapshot["config"])
        current_version = self.config_version
        current_completed = len(self.batches)
        data_match = snapshot.get("data_digest") == self.assets.data_digest
        parameter_match = snapshot.get("parameter_digest") == self.assets.parameter_digest

        self._clear_active(reason=f"replaced_by_restore:{snapshot_config.config_version}", record_stale=True)
        self._transition_old = None
        self._transition = None
        self.last_replay = None
        self.config = snapshot_config

        plans = plan_batches(self.assets, snapshot_config) if data_match and parameter_match else []
        expected_signature = [list(plan.sample_ids) for plan in plans]
        snapshot_signature = [
            list(item.get("sample_ids", []))
            for item in snapshot.get("batch_plan", [])
            if isinstance(item, dict)
        ]
        plan_match = bool(plans) and snapshot_signature == expected_signature
        config_match_before = current_version == snapshot_config.config_version

        restore_record: dict[str, Any] = {
            "status": "restored",
            "code": None,
            "snapshot_path": relative_path(path, self.snapshot_dir.parent),
            "readonly_backup": relative_path(backup, self.snapshot_dir.parent),
            "current_config_version_before": current_version,
            "snapshot_config_version": snapshot_config.config_version,
            "config_version_changed": current_version != snapshot_config.config_version,
            "config_version_match_current": config_match_before,
            "data_digest_match": data_match,
            "parameter_digest_match": parameter_match,
            "batch_plan_match": plan_match,
            "cleared_current_batches": current_completed,
            "loaded_completed_batches": 0,
            "next_batch_index": 1,
            "replaced_state_wholesale": True,
        }

        if not data_match or not parameter_match:
            self.plans = []
            self.status = "incompatible"
            restore_record.update(
                {
                    "status": "incompatible",
                    "code": "E_DATA",
                    "message": "快照摘要与当前会话数据/参数摘要不一致，已清空当前结果但未加载旧结论",
                    "snapshot_data_digest": snapshot.get("data_digest"),
                    "current_data_digest": self.assets.data_digest,
                    "snapshot_parameter_digest": snapshot.get("parameter_digest"),
                    "current_parameter_digest": self.assets.parameter_digest,
                }
            )
            self.last_restore = restore_record
            return restore_record

        self.plans = plans
        if not plan_match:
            self.status = "incompatible"
            restore_record.update(
                {
                    "status": "incompatible",
                    "code": "E_REPLAY",
                    "message": "快照批划分无法由当前配置与样本复现，未加载旧 batches",
                    "snapshot_batch_plan": snapshot_signature,
                    "expected_batch_plan": expected_signature,
                }
            )
            self.last_restore = restore_record
            return restore_record

        for item in snapshot.get("completed_batches", []):
            plan_index = int(item["batch_index"]) - 1
            expected = self.plans[plan_index]
            if list(item.get("sample_ids", [])) != list(expected.sample_ids):
                self.batches.clear()
                self.status = "incompatible"
                restore_record.update(
                    {
                        "status": "incompatible",
                        "code": "E_REPLAY",
                        "message": "快照批次内样本顺序与重算批计划不一致",
                        "batch_index": plan_index + 1,
                    }
                )
                self.last_restore = restore_record
                return restore_record
            self.batches.append(copy.deepcopy(item))

        self.status = "complete" if self.complete else "interrupted"
        restore_record.update(
            {
                "loaded_completed_batches": len(self.batches),
                "next_batch_index": self.next_batch_index,
                "status": self.status,
            }
        )
        self.last_restore = restore_record
        return restore_record

    def snapshot_files(self) -> list[str]:
        if not self.snapshot_dir.exists():
            return []
        files = sorted(
            path
            for path in self.snapshot_dir.glob("*.json")
            if path.parent == self.snapshot_dir
        )
        return [relative_path(path, self.snapshot_dir.parent) for path in files]

    def _compare_batch_replay(
        self,
        recorded: Mapping[str, Any],
        replayed: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        batch_index = int(recorded.get("batch_index", 0))
        batch_id = str(recorded.get("batch_id", ""))
        recorded_sample_ids = list(recorded.get("sample_ids", []))
        replayed_sample_ids = list(replayed.get("sample_ids", []))
        if recorded_sample_ids != replayed_sample_ids:
            return {
                "batch_index": batch_index,
                "batch_id": batch_id,
                "sample_id": next(
                    (
                        recorded_sample_ids[index]
                        for index in range(min(len(recorded_sample_ids), len(replayed_sample_ids)))
                        if recorded_sample_ids[index] != replayed_sample_ids[index]
                    ),
                    recorded_sample_ids[0] if recorded_sample_ids else None,
                ),
                "layer": "batch_plan",
                "field": "sample_ids",
                "snapshot": recorded_sample_ids,
                "replayed": replayed_sample_ids,
            }

        recorded_samples = {item["sample_id"]: item for item in recorded.get("samples", [])}
        replayed_samples = {item["sample_id"]: item for item in replayed.get("samples", [])}
        for sample_id in recorded_sample_ids:
            old_sample = recorded_samples[sample_id]
            new_sample = replayed_samples[sample_id]
            old_output = list(old_sample.get("output", []))
            new_output = list(new_sample.get("output", []))
            for index in range(min(len(old_output), len(new_output))):
                if float32_bits(old_output[index]) != float32_bits(new_output[index]):
                    return {
                        "batch_index": batch_index,
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "layer": "output",
                        "field": f"output[{index}]",
                        "snapshot": float32_bits(old_output[index]),
                        "replayed": float32_bits(new_output[index]),
                    }
            if len(old_output) != len(new_output):
                return {
                    "batch_index": batch_index,
                    "batch_id": batch_id,
                    "sample_id": sample_id,
                    "layer": "output",
                    "field": "length",
                    "snapshot": len(old_output),
                    "replayed": len(new_output),
                }

            old_reference = list(old_sample.get("reference_output", []))
            new_reference = list(new_sample.get("reference_output", []))
            for index in range(min(len(old_reference), len(new_reference))):
                if float32_bits(old_reference[index]) != float32_bits(new_reference[index]):
                    return {
                        "batch_index": batch_index,
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "layer": "output",
                        "field": f"reference_output[{index}]",
                        "snapshot": float32_bits(old_reference[index]),
                        "replayed": float32_bits(new_reference[index]),
                    }

            old_q = old_sample.get("q_output")
            new_q = new_sample.get("q_output")
            if old_q != new_q:
                return {
                    "batch_index": batch_index,
                    "batch_id": batch_id,
                    "sample_id": sample_id,
                    "layer": "output",
                    "field": "q_output",
                    "snapshot": old_q,
                    "replayed": new_q,
                }

            old_layers = old_sample.get("diagnostics", {}).get("layers", [])
            new_layers = new_sample.get("diagnostics", {}).get("layers", [])
            new_layer_map = {item.get("layer"): item for item in new_layers}
            for old_layer in old_layers:
                layer_name = str(old_layer.get("layer"))
                new_layer = new_layer_map.get(layer_name)
                if new_layer is None:
                    return {
                        "batch_index": batch_index,
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "layer": layer_name,
                        "field": "layer_presence",
                        "snapshot": True,
                        "replayed": False,
                    }
                mismatch = self._compare_layer(layer_name, old_layer, new_layer)
                if mismatch is not None:
                    return {
                        "batch_index": batch_index,
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        **mismatch,
                    }

            old_diag = old_sample.get("diagnostics", {})
            new_diag = new_sample.get("diagnostics", {})
            for field in (
                "output_relative_deviation",
                "dominant_layer",
                "dominant_error_share",
                "dominant_cumulative_share",
                "drift_first_exceedance",
            ):
                if not self._equal_json_value(old_diag.get(field), new_diag.get(field)):
                    return {
                        "batch_index": batch_index,
                        "batch_id": batch_id,
                        "sample_id": sample_id,
                        "layer": "attribution_summary",
                        "field": field,
                        "snapshot": old_diag.get(field),
                        "replayed": new_diag.get(field),
                    }

        if recorded.get("output_summary_sha256") != replayed.get("output_summary_sha256"):
            return {
                "batch_index": batch_index,
                "batch_id": batch_id,
                "sample_id": recorded_sample_ids[0] if recorded_sample_ids else None,
                "layer": "output",
                "field": "output_summary_sha256",
                "snapshot": recorded.get("output_summary_sha256"),
                "replayed": replayed.get("output_summary_sha256"),
            }
        return None

    @staticmethod
    def _equal_json_value(left: Any, right: Any) -> bool:
        if isinstance(left, dict) and isinstance(right, dict):
            if list(left.keys()) != list(right.keys()):
                return False
            return all(
                InferenceSession._equal_json_value(left[key], right[key]) for key in left
            )
        if isinstance(left, list) and isinstance(right, list):
            return len(left) == len(right) and all(
                InferenceSession._equal_json_value(a, b) for a, b in zip(left, right)
            )
        return bits_equal(left, right)

    def _compare_layer(
        self,
        layer_name: str,
        recorded: Mapping[str, Any],
        replayed: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        for field in (
            "cumulative_error_bound_grid",
            "local_error_bound_grid",
            "accumulation_saturation_count",
            "bounds_valid",
            "abs_error",
            "relative_error",
            "relative_delta",
            "increase",
            "increase_share",
            "cumulative_share",
        ):
            if not self._equal_json_value(recorded.get(field), replayed.get(field)):
                return {
                    "layer": layer_name,
                    "field": field,
                    "snapshot": recorded.get(field),
                    "replayed": replayed.get(field),
                }
        return None

    def replay_snapshot(
        self,
        path: str | Path,
        *,
        expected_config: BatchConfig | None = None,
    ) -> dict[str, Any]:
        snapshot = load_snapshot(path)
        snapshot_config = BatchConfig.from_dict(snapshot["config"])
        data_match = snapshot.get("data_digest") == self.assets.data_digest
        parameter_match = snapshot.get("parameter_digest") == self.assets.parameter_digest
        unreproducible: list[dict[str, Any]] = []
        if expected_config is not None:
            if expected_config.config_version != snapshot_config.config_version:
                unreproducible.append(
                    {
                        "item": "config_version",
                        "snapshot": snapshot_config.config_version,
                        "current": expected_config.config_version,
                    }
                )
            if expected_config.quant_scale != snapshot_config.quant_scale:
                unreproducible.append(
                    {
                        "item": "quant_scale",
                        "snapshot": snapshot_config.quant_scale,
                        "current": expected_config.quant_scale,
                    }
                )
            if expected_config.precision_mode != snapshot_config.precision_mode:
                unreproducible.append(
                    {
                        "item": "precision_mode",
                        "snapshot": snapshot_config.precision_mode,
                        "current": expected_config.precision_mode,
                    }
                )
        if not data_match:
            unreproducible.append(
                {
                    "item": "data_digest",
                    "snapshot": snapshot.get("data_digest"),
                    "current": self.assets.data_digest,
                }
            )
        if not parameter_match:
            unreproducible.append(
                {
                    "item": "parameter_digest",
                    "snapshot": snapshot.get("parameter_digest"),
                    "current": self.assets.parameter_digest,
                }
            )

        plans = plan_batches(self.assets, snapshot_config) if data_match and parameter_match else []
        expected_signature = [list(plan.sample_ids) for plan in plans]
        recorded_signature = [
            list(item.get("sample_ids", []))
            for item in snapshot.get("batch_plan", [])
            if isinstance(item, dict)
        ]
        if plans and expected_signature != recorded_signature:
            unreproducible.append(
                {
                    "item": "batch_plan",
                    "snapshot": recorded_signature,
                    "current": expected_signature,
                }
            )

        report: dict[str, Any] = {
            "status": "replayed",
            "code": None,
            "snapshot_path": relative_path(path, self.snapshot_dir.parent),
            "snapshot_config_version": snapshot_config.config_version,
            "expected_config_version": (
                expected_config.config_version if expected_config is not None else None
            ),
            "data_digest_match": data_match,
            "parameter_digest_match": parameter_match,
            "batch_plan_match": not any(item["item"] == "batch_plan" for item in unreproducible),
            "checked_batches": 0,
            "checked_samples": 0,
            "first_mismatch": None,
            "unreproducible_items": unreproducible,
        }
        if unreproducible:
            report["status"] = "failed"
            report["code"] = "E_REPLAY"
            self.last_replay = report
            return report

        desired_plan = plans[: len(snapshot.get("completed_batches", []))]
        for plan, recorded in zip(desired_plan, snapshot.get("completed_batches", [])):
            replayed = run_batch(
                self.assets,
                snapshot_config,
                plan,
                prepare_fixed_model(self.assets, snapshot_config)
                if snapshot_config.precision_mode == "fixed"
                else None,
            )
            report["checked_batches"] += 1
            report["checked_samples"] += len(replayed.get("samples", []))
            mismatch = self._compare_batch_replay(recorded, replayed)
            if mismatch is not None:
                report.update(
                    {
                        "status": "failed",
                        "code": "E_REPLAY",
                        "first_mismatch": mismatch,
                    }
                )
                self.last_replay = report
                return report

        report["code"] = None
        report["status"] = "verified"
        self.last_replay = report
        return report

    def _ensure_transition(self) -> None:
        if self._transition is not None or self._transition_old is None or self.config is None:
            return
        self._transition = compare_versions(
            self._transition_old,
            self.config,
            self.plans,
            self.batches,
        )

    def build_report(self) -> dict[str, Any]:
        if self.config is None:
            raise WorkbenchError("E_CONFIG", "会话尚未加载配置，无法生成报告")
        self._ensure_transition()
        business_codes = sorted(
            {
                code
                for batch in self.batches
                for code in batch.get("business_codes", [])
            }
        )
        if self.last_restore and self.last_restore.get("code"):
            business_codes = sorted({*business_codes, self.last_restore["code"]})
        if self.last_replay and self.last_replay.get("code"):
            business_codes = sorted({*business_codes, self.last_replay["code"]})

        batches = copy.deepcopy(self.batches)
        layer_summary: dict[str, dict[str, float]] = {}
        if batches:
            for layer_name in self.assets.layer_names:
                bounds: list[float] = []
                relative_errors: list[float] = []
                for batch in batches:
                    for sample in batch.get("samples", []):
                        for layer in sample.get("diagnostics", {}).get("layers", []):
                            if layer.get("layer") == layer_name:
                                bounds.append(float(layer.get("cumulative_error_bound_grid", 0.0)))
                                relative_errors.append(float(layer.get("relative_error", 0.0)))
                layer_summary[layer_name] = {
                    "max_cumulative_error_bound_grid": max(bounds, default=0.0),
                    "max_relative_error": max(relative_errors, default=0.0),
                }

        report = {
            "schema": REPORT_SCHEMA,
            "data": {
                "generation": dict(self.assets.generation),
                "data_digest": self.assets.data_digest,
                "parameter_digest": self.assets.parameter_digest,
                "summary_mismatch_policy": "reject_whole_run_with_E_DATA",
            },
            "configuration": self.config.to_dict(),
            "config_version": self.config.config_version,
            "state": {
                "status": self.status,
                "completed_batches": len(self.batches),
                "total_batches": len(self.plans),
                "next_batch_index": self.next_batch_index if not self.complete else None,
                "complete": self.complete,
                "whole_version_replacement": True,
            },
            "batch_plan": [plan.to_dict() for plan in self.plans],
            "batches": batches,
            "diagnostics": {
                "bound_method": BOUND_METHOD,
                "layer_summary": layer_summary,
                "summary": {
                    "drift_batches": sum(1 for batch in batches if batch.get("drift")),
                    "max_output_relative_deviation": max(
                        (
                            float(batch.get("max_output_relative_deviation", 0.0))
                            for batch in batches
                        ),
                        default=0.0,
                    ),
                    "max_cumulative_error_bound_grid": max(
                        (
                            int(batch.get("max_cumulative_error_bound_grid", 0))
                            for batch in batches
                        ),
                        default=0,
                    ),
                },
            },
            "version_transition": self._transition,
            "stale_versions": copy.deepcopy(self._stale_versions),
            "restore": copy.deepcopy(self.last_restore),
            "replay": copy.deepcopy(self.last_replay),
            "snapshots": self.snapshot_files(),
            "business_codes": business_codes,
        }
        return report

    def write_report(self, path: str | Path) -> Path:
        return atomic_write_json(path, self.build_report())


def _format_float(value: float) -> str:
    if not math.isfinite(float(value)):
        return str(value)
    return f"{float(value):.8g}"


def human_summary(report: Mapping[str, Any]) -> str:
    data = report.get("data", {})
    configuration = report.get("configuration", {})
    state = report.get("state", {})
    lines = [
        f"data_digest={data.get('data_digest')}",
        f"parameter_digest={data.get('parameter_digest')}",
        (
            "config_version={version} precision={mode} batch_size={batch_size} "
            "sort={sort_strategy} quant_scale={quant_scale}"
        ).format(
            version=report.get("config_version"),
            mode=configuration.get("precision_mode"),
            batch_size=configuration.get("batch_size"),
            sort_strategy=configuration.get("sort_strategy"),
            quant_scale=configuration.get("quant_scale"),
        ),
        (
            "state={status} completed={completed_batches}/{total_batches} "
            "business_codes={codes}"
        ).format(
            status=state.get("status"),
            completed_batches=state.get("completed_batches"),
            total_batches=state.get("total_batches"),
            codes=",".join(report.get("business_codes", [])) or "OK",
        ),
    ]
    for batch in report.get("batches", []):
        lines.append(
            "batch={batch_id} samples={samples} drift={drift} "
            "max_output_relative_deviation={deviation} first_drift={first}"
        .format(
            batch_id=batch.get("batch_id"),
            samples=",".join(batch.get("sample_ids", [])),
            drift="E_DRIFT" if batch.get("drift") else "none",
            deviation=_format_float(batch.get("max_output_relative_deviation", 0.0)),
            first=(
                "{}:{}".format(
                    batch["first_drift"].get("sample_id"), batch["first_drift"].get("layer")
                )
                if batch.get("first_drift")
                else "none"
            ),
        )
        )
        batch_layers: dict[str, dict[str, float]] = {
            layer_name: {"max_bound_grid": 0.0, "max_relative_error": 0.0}
            for layer_name in report.get("diagnostics", {}).get("layer_summary", {})
        }
        for sample in batch.get("samples", []):
            for layer in sample.get("diagnostics", {}).get("layers", []):
                layer_name = layer.get("layer")
                if layer_name not in batch_layers:
                    continue
                batch_layers[layer_name]["max_bound_grid"] = max(
                    batch_layers[layer_name]["max_bound_grid"],
                    float(layer.get("cumulative_error_bound_grid", 0.0)),
                )
                batch_layers[layer_name]["max_relative_error"] = max(
                    batch_layers[layer_name]["max_relative_error"],
                    float(layer.get("relative_error", 0.0)),
                )
        for layer_name, summary in batch_layers.items():
            lines.append(
                "  layer={} max_bound_grid={} max_relative_error={}".format(
                    layer_name,
                    _format_float(summary.get("max_bound_grid", 0.0)),
                    _format_float(summary.get("max_relative_error", 0.0)),
                )
            )
    transition = report.get("version_transition")
    if transition:
        lines.append(
            "version_diff={old}->{new} changed_batches={batches} changed_layers={layers} drift_direction={drift}".format(
                old=transition.get("old_config_version"),
                new=transition.get("new_config_version"),
                batches=",".join(str(item) for item in transition.get("changed_batch_indices", [])) or "none",
                layers=",".join(transition.get("changed_layers", [])) or "none",
                drift=transition.get("drift_direction"),
            )
        )
    restore = report.get("restore")
    if restore:
        lines.append(
            "restore={status} snapshot_version={version} loaded_batches={loaded} backup={backup}".format(
                status=restore.get("status"),
                version=restore.get("snapshot_config_version"),
                loaded=restore.get("loaded_completed_batches", 0),
                backup=restore.get("readonly_backup"),
            )
        )
    replay = report.get("replay")
    if replay:
        lines.append(
            "replay={status} checked_batches={batches} checked_samples={samples} first_mismatch={mismatch}".format(
                status=replay.get("status"),
                batches=replay.get("checked_batches"),
                samples=replay.get("checked_samples"),
                mismatch=replay.get("first_mismatch") or "none",
            )
        )
    return "\n".join(lines) + "\n"
