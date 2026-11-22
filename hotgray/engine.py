from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .dependency import InvalidationReport, analyze_invalidation
from .errors import BizError, ErrorCode, fail
from .inference import binding_stable_view, compute_inference
from .models import (
    Samples,
    load_samples,
    parse_clock_steps,
    parse_manifest,
    parse_patch,
    parse_plan,
    parse_replicas,
)
from .shards import (
    ShardMaterializer,
    compare_target_directories,
    compose_patch_shards,
)
from .storage import SCHEMA_VERSION, Store, row_to_dict, rows_to_dicts
from .util import (
    atomic_write_bytes,
    canonical_json,
    int_field,
    resolve_path,
    sha256_text,
)

ROUND_FINAL_STATES = {"rolled_back", "rollback_not_achieved"}
ROUND_ROLLBACK_STATES = {"rolling_back", "rolling_back_failed"}
ROUND_GRAY_STATES = {"ready", "gray", "released"}
TRANSITIONS: dict[str, set[str]] = {
    "pending": {"loading", "rolling_back"},
    "loading": {"prewarmed", "rolling_back"},
    "prewarmed": {"gray", "rolling_back"},
    "gray": {"gray", "rolling_back"},
    "rolling_back": {"rolling_back", "rolled_back", "rollback_not_achieved"},
    "rolled_back": set(),
    "rollback_not_achieved": set(),
}


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


class Engine:
    def __init__(self, store: Store, samples: Samples) -> None:
        self.store = store
        self.samples = samples
        self.manifest = samples.manifest
        self.plan = samples.plan
        self.patch = samples.patch
        self.replicas = samples.replicas
        self.materializer = ShardMaterializer(store.workspace, samples.root)

    @classmethod
    def initialize(
        cls,
        *,
        workspace: str | Path,
        samples_dir: str | Path,
        db_name: str = "state.db",
    ) -> "Engine":
        samples = load_samples(samples_dir)
        store = Store(Path(workspace) / db_name)
        if store.db_path.exists():
            fail(
                ErrorCode.CONFLICT,
                f"持久化数据库已存在: {store.db_path}",
                path=str(store.db_path),
            )
        materializer = ShardMaterializer(store.workspace, samples.root)
        blob_rows = cls._prepare_initial_blobs(samples, materializer)
        statements = cls._initial_statements(samples, blob_rows)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "samples_root": str(samples.root),
            "manifest_json": canonical_json(samples.manifest.raw),
            "replicas_json": canonical_json([replica.raw for replica in samples.replicas]),
            "plan_json": canonical_json(samples.plan.raw),
            "patch_json": canonical_json(samples.patch.raw),
            "clock_steps_json": canonical_json(
                {
                    name: [
                        {"at_ms": action.at_ms, "command": action.command, "args": dict(action.args)}
                        for action in actions
                    ]
                    for name, actions in sorted(samples.clock_scenarios.items())
                }
            ),
            "round_id": samples.plan.round_id,
        }
        store.initialize(meta=meta, statements=statements)
        return cls.open(store.db_path)

    @staticmethod
    def _verify_all_sample_shards(samples: Samples, materializer: ShardMaterializer) -> None:
        for version in sorted(samples.manifest.models):
            model = samples.manifest.models[version]
            for layer in model.layers:
                for shard in layer.shards:
                    if shard.path is None:
                        fail(
                            ErrorCode.MISSING_SHARD,
                            f"版本 {version} 层 {layer.name} 分片 {shard.index} 缺少样本文件路径",
                            version=version,
                            layer=layer.name,
                            shard_index=shard.index,
                        )
                    materializer.verify_file(
                        shard.digest,
                        resolve_path(samples.root, shard.path),
                        version=version,
                        layer=layer.name,
                        shard_index=shard.index,
                    )

    @classmethod
    def _prepare_initial_blobs(
        cls,
        samples: Samples,
        materializer: ShardMaterializer,
    ) -> list[tuple[str, str, int, int]]:
        cls._verify_all_sample_shards(samples, materializer)
        model = samples.manifest.model(samples.plan.baseline_version)
        rows: dict[str, tuple[str, str, int, int]] = {}
        for layer in model.layers:
            for shard in layer.shards:
                if shard.digest in rows:
                    continue
                if shard.path is None:
                    fail(
                        ErrorCode.MISSING_SHARD,
                        f"基线分片 {layer.name}#{shard.index} 缺少文件路径",
                        baseline_version=model.version,
                        layer=layer.name,
                        shard_index=shard.index,
                    )
                data = materializer.verify_file(
                    shard.digest,
                    resolve_path(samples.root, shard.path),
                    version=model.version,
                    layer=layer.name,
                    shard_index=shard.index,
                )
                target = materializer.blob_path(shard.digest)
                if target.is_file():
                    materializer.verify_file(
                        shard.digest,
                        target,
                        version=model.version,
                        layer=layer.name,
                        shard_index=shard.index,
                    )
                else:
                    atomic_write_bytes(target, data)
                rows[shard.digest] = (shard.digest, str(target), len(data), 0)
        return [rows[digest] for digest in sorted(rows)]

    @classmethod
    def _initial_statements(
        cls,
        samples: Samples,
        blob_rows: Sequence[tuple[str, str, int, int]],
    ) -> list[tuple[str, Sequence[Any]]]:
        statements: list[tuple[str, Sequence[Any]]] = []
        for resource in sorted(samples.manifest.resources.values(), key=lambda item: item.resource_id):
            statements.append(
                (
                    "INSERT INTO resources(resource_id, kind, revision, digest, depends_on_json, "
                    "params_json, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        resource.resource_id,
                        resource.kind,
                        resource.revision,
                        resource.digest,
                        canonical_json(list(resource.depends_on)),
                        canonical_json(dict(resource.params)),
                        canonical_json(dict(resource.raw)),
                    ),
                )
            )
        for model in sorted(samples.manifest.models.values(), key=lambda item: item.version):
            statements.append(
                (
                    "INSERT INTO model_versions(version, raw_json, layers_json, preprocess, "
                    "postprocess, feature_table, upstream_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        model.version,
                        canonical_json(dict(model.raw)),
                        canonical_json([layer.as_dict() for layer in model.layers]),
                        model.preprocess,
                        model.postprocess,
                        model.feature_table,
                        model.upstream_model,
                    ),
                )
            )
        baseline = samples.manifest.model(samples.plan.baseline_version)
        for layer in baseline.layers:
            for shard in layer.shards:
                statements.append(
                    (
                        "INSERT INTO shards(version, layer, shard_index, byte_start, byte_end, "
                        "digest, declared_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            baseline.version,
                            layer.name,
                            shard.index,
                            shard.byte_start,
                            shard.byte_end,
                            shard.digest,
                            shard.path,
                        ),
                    )
                )
        for digest, path, size, created_at in blob_rows:
            statements.append(
                (
                    "INSERT INTO materializations(digest, path, size, created_at_ms) "
                    "VALUES (?, ?, ?, ?)",
                    (digest, path, size, created_at),
                )
            )
        baseline_binding = samples.manifest.version_binding(baseline.version)
        statements.append(
            (
                "INSERT INTO version_bindings(version, binding_json, binding_digest, committed, "
                "created_at_ms) VALUES (?, ?, ?, 1, ?)",
                (
                    baseline.version,
                    canonical_json(baseline_binding),
                    baseline_binding["binding_digest"],
                    0,
                ),
            )
        )
        eligible = set(samples.plan.replica_ids)
        for replica in samples.replicas:
            loaded_shards = {
                layer: {str(index): digest for (layer_name, index), digest in sorted(replica.loaded_shards.items()) if layer_name == layer}
                for layer in sorted({layer_name for layer_name, _ in replica.loaded_shards})
            }
            statements.append(
                (
                    "INSERT INTO replicas(replica_id, loaded_version, loaded_shards_json, "
                    "baseline_state, baseline_ratio, eligible) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        replica.replica_id,
                        replica.loaded_version,
                        canonical_json(loaded_shards),
                        replica.warmup_state,
                        replica.gray_ratio,
                        1 if replica.replica_id in eligible else 0,
                    ),
                )
            )
        statements.append(
            (
                "INSERT INTO rounds(round_id, plan_json, patch_json, baseline_version, "
                "target_version, status, current_level, current_ratio, logical_watermark_ms, "
                "created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, 'prepared', 0, 0.0, 0, 0, 0)",
                (
                    samples.plan.round_id,
                    canonical_json(samples.plan.raw),
                    canonical_json(samples.patch.raw),
                    samples.plan.baseline_version,
                    samples.plan.target_version,
                ),
            )
        )
        for replica_id in samples.plan.replica_ids:
            statements.append(
                (
                    "INSERT INTO round_replicas(round_id, replica_id, state, serving_version, "
                    "stage_version, gray_ratio, updated_at_ms) VALUES (?, ?, 'pending', ?, ?, 0.0, 0)",
                    (
                        samples.plan.round_id,
                        replica_id,
                        samples.plan.baseline_version,
                        samples.plan.target_version,
                    ),
                )
            )
        statements.append(
            (
                "INSERT INTO clock_state(id, watermark_ms, tolerance_ms, last_requested_ms) "
                "VALUES (1, 0, ?, 0)",
                (samples.plan.clock_tolerance_ms,),
            )
        )
        statements.append(
            (
                "INSERT INTO events(round_id, at_ms, event_type, payload_json) "
                "VALUES (?, 0, 'round_prepared', ?)",
                (
                    samples.plan.round_id,
                    canonical_json(
                        {
                            "baseline_version": samples.plan.baseline_version,
                            "target_version": samples.plan.target_version,
                            "replica_ids": list(samples.plan.replica_ids),
                            "patch_id": samples.patch.patch_id,
                        }
                    ),
                ),
            )
        )
        return statements

    @classmethod
    def open(cls, db_path: str | Path) -> "Engine":
        store = Store(db_path)
        store.require_initialized()
        samples_root = store.get_meta("samples_root")
        if not samples_root:
            fail(ErrorCode.PERSISTENCE_ERROR, "数据库缺少 samples_root 元数据")
        manifest_raw = store.get_meta_json("manifest_json")
        replicas_raw = store.get_meta_json("replicas_json")
        plan_raw = store.get_meta_json("plan_json")
        patch_raw = store.get_meta_json("patch_json")
        clock_raw = store.get_meta_json("clock_steps_json")
        manifest = parse_manifest(manifest_raw, samples_root)
        replicas = parse_replicas(replicas_raw, manifest)
        plan = parse_plan(plan_raw, manifest, replicas)
        patch = parse_patch(
            patch_raw,
            manifest,
            samples_root=samples_root,
            verify_blob_paths=False,
        )
        scenarios = parse_clock_steps(clock_raw)
        samples = Samples(
            root=Path(samples_root),
            manifest=manifest,
            replicas=replicas,
            plan=plan,
            patch=patch,
            clock_scenarios=scenarios,
        )
        return cls(store, samples)

    def _clock(
        self,
        connection: sqlite3.Connection,
        requested_ms: int,
        *,
        tolerance_ms: int | None = None,
    ) -> int:
        requested = int_field(requested_ms, "logical_ms", minimum=0)
        row = connection.execute(
            "SELECT watermark_ms, tolerance_ms FROM clock_state WHERE id = 1"
        ).fetchone()
        if row is None:
            fail(ErrorCode.PERSISTENCE_ERROR, "逻辑时钟状态缺失")
        tolerance = self.plan.clock_tolerance_ms if tolerance_ms is None else tolerance_ms
        watermark = int(row["watermark_ms"])
        if requested < watermark - tolerance:
            fail(
                ErrorCode.CLOCK_REGRESSION,
                f"逻辑时钟回拨超过容差: {requested} < {watermark} - {tolerance}",
                expected=watermark,
                actual=requested,
                tolerance=tolerance,
            )
        effective = max(watermark, requested)
        connection.execute(
            "UPDATE clock_state SET watermark_ms = ?, last_requested_ms = ? WHERE id = 1",
            (effective, requested),
        )
        return effective

    def _round(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM rounds WHERE round_id = ?",
            (self.plan.round_id,),
        ).fetchone()
        if row is None:
            fail(
                ErrorCode.NOT_FOUND,
                f"更新轮次 {self.plan.round_id} 不存在",
                round_id=self.plan.round_id,
            )
        return row_to_dict(row) or {}

    def _round_replicas(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM round_replicas WHERE round_id = ? ORDER BY replica_id",
            (self.plan.round_id,),
        ).fetchall()
        return rows_to_dicts(rows)

    def _set_round_watermark(
        self,
        connection: sqlite3.Connection,
        at_ms: int,
        *,
        status: str | None = None,
    ) -> None:
        if status is None:
            connection.execute(
                "UPDATE rounds SET logical_watermark_ms = ?, updated_at_ms = ? "
                "WHERE round_id = ?",
                (at_ms, at_ms, self.plan.round_id),
            )
        else:
            connection.execute(
                "UPDATE rounds SET logical_watermark_ms = ?, updated_at_ms = ?, status = ? "
                "WHERE round_id = ?",
                (at_ms, at_ms, status, self.plan.round_id),
            )

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO events(round_id, at_ms, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (self.plan.round_id, at_ms, event_type, canonical_json(dict(payload))),
        )

    def _diagnostic(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
        code: ErrorCode,
        message: str,
        details: Mapping[str, Any],
    ) -> None:
        encoded = canonical_json(dict(details))
        exists = connection.execute(
            "SELECT 1 FROM diagnostics WHERE round_id = ? AND at_ms = ? AND code = ? "
            "AND message = ? AND details_json = ? LIMIT 1",
            (self.plan.round_id, at_ms, code.value, message, encoded),
        ).fetchone()
        if exists is not None:
            return
        connection.execute(
            "INSERT INTO diagnostics(round_id, at_ms, code, name, message, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.plan.round_id,
                at_ms,
                code.value,
                code.name,
                message,
                encoded,
            ),
        )

    def _step(
        self,
        connection: sqlite3.Connection,
        step_key: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM round_steps WHERE round_id = ? AND step_key = ?",
            (self.plan.round_id, step_key),
        ).fetchone()
        return row_to_dict(row)

    def _record_step(
        self,
        connection: sqlite3.Connection,
        *,
        step_key: str,
        at_ms: int,
        payload: Mapping[str, Any],
        status: str = "committed",
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO round_steps(round_id, step_key, status, at_ms, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self.plan.round_id,
                step_key,
                status,
                at_ms,
                canonical_json(dict(payload)),
            ),
        )

    def _insert_decisions(
        self,
        connection: sqlite3.Connection,
        report: InvalidationReport,
    ) -> None:
        for decision in report.decisions:
            connection.execute(
                "INSERT OR IGNORE INTO invalidations(round_id, replica_id, cache_type, version, "
                "decision, trigger, path_json, source_json, at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.plan.round_id,
                    decision.replica_id,
                    decision.cache_type,
                    decision.version,
                    decision.decision,
                    decision.trigger,
                    canonical_json(list(decision.path)),
                    canonical_json(dict(decision.source or {})),
                    decision.at_ms,
                ),
            )

    def _target_not_ready(
        self,
        connection: sqlite3.Connection,
        *,
        verify_content: bool,
        include_replicas: bool = False,
        required_replica_states: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        target_version = self.plan.target_version
        issues: list[dict[str, Any]] = []
        binding_row = connection.execute(
            "SELECT * FROM version_bindings WHERE version = ? AND committed = 1",
            (target_version,),
        ).fetchone()
        if binding_row is None:
            return [
                {
                    "type": "target_binding",
                    "version": target_version,
                    "message": "目标版本组合尚未完整提交",
                }
            ]
        binding = _loads(binding_row["binding_json"], {})
        binding_chain = binding.get("shard_chain", [])
        model = self.manifest.model(target_version)
        expected_chain = model.shard_chain()
        if binding_chain != expected_chain:
            issues.append(
                {
                    "type": "binding_chain_mismatch",
                    "version": target_version,
                    "message": "已提交版本绑定摘要链与目标清单不一致",
                }
            )
        shard_rows = connection.execute(
            "SELECT * FROM shards WHERE version = ? ORDER BY layer, shard_index",
            (target_version,),
        ).fetchall()
        rows_by_key = {
            (row["layer"], int(row["shard_index"])): row for row in shard_rows
        }
        for layer in model.layers:
            for shard in layer.shards:
                key = (layer.name, shard.index)
                row = rows_by_key.get(key)
                if row is None:
                    issues.append(
                        {
                            "type": "missing_shard",
                            "version": target_version,
                            "layer": layer.name,
                            "shard_index": shard.index,
                            "message": "目标分片未提交",
                        }
                    )
                    continue
                if (
                    int(row["byte_start"]),
                    int(row["byte_end"]),
                    row["digest"],
                ) != (shard.byte_start, shard.byte_end, shard.digest):
                    issues.append(
                        {
                            "type": "shard_metadata_mismatch",
                            "version": target_version,
                            "layer": layer.name,
                            "shard_index": shard.index,
                            "expected": shard.digest,
                            "actual": row["digest"],
                        }
                    )
                materialization = connection.execute(
                    "SELECT path, size FROM materializations WHERE digest = ?",
                    (shard.digest,),
                ).fetchone()
                if materialization is None:
                    issues.append(
                        {
                            "type": "missing_materialization",
                            "version": target_version,
                            "layer": layer.name,
                            "shard_index": shard.index,
                            "expected": shard.digest,
                        }
                    )
                    continue
                path = Path(materialization["path"])
                if not path.is_file():
                    issues.append(
                        {
                            "type": "missing_blob",
                            "version": target_version,
                            "layer": layer.name,
                            "shard_index": shard.index,
                            "path": str(path),
                        }
                    )
                elif verify_content:
                    try:
                        self.materializer.verify_file(
                            shard.digest,
                            path,
                            version=target_version,
                            layer=layer.name,
                            shard_index=shard.index,
                        )
                    except BizError as exc:
                        issues.append(
                            {
                                "type": "digest_mismatch",
                                "version": target_version,
                                "layer": layer.name,
                                "shard_index": shard.index,
                                "error_code": exc.code.value,
                                "message": exc.message,
                            }
                        )
        for resource_id, snapshot in sorted(binding.get("resources", {}).items()):
            row = connection.execute(
                "SELECT digest FROM resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if row is None:
                issues.append(
                    {
                        "type": "missing_config",
                        "version": target_version,
                        "resource_id": resource_id,
                    }
                )
            elif row["digest"] != snapshot.get("digest"):
                issues.append(
                    {
                        "type": "config_mismatch",
                        "version": target_version,
                        "resource_id": resource_id,
                        "expected": snapshot.get("digest"),
                        "actual": row["digest"],
                    }
                )
        if include_replicas:
            required = set(required_replica_states)
            if required:
                for replica in self._round_replicas(connection):
                    if replica["state"] not in required:
                        issues.append(
                            {
                                "type": "replica_state",
                                "version": target_version,
                                "replica_id": replica["replica_id"],
                                "actual": replica["state"],
                                "expected": sorted(required),
                            }
                        )
        issues.sort(
            key=lambda issue: (
                str(issue.get("type")),
                str(issue.get("replica_id", "")),
                str(issue.get("layer", "")),
                int(issue.get("shard_index", -1)),
                str(issue.get("resource_id", "")),
            )
        )
        return issues

    def apply_patch(
        self,
        *,
        at_ms: int,
        crash_after_materialize: int | None = None,
        materialize_callback: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            round_state = self._round(connection)
            if round_state["status"] in ROUND_FINAL_STATES | ROUND_ROLLBACK_STATES:
                fail(
                    ErrorCode.CONFLICT,
                    f"轮次当前状态 {round_state['status']} 不允许提交补丁",
                    round_id=self.plan.round_id,
                    actual=round_state["status"],
                )
            existing_step = self._step(connection, "patch.applied")
            target_binding_row = connection.execute(
                "SELECT * FROM version_bindings WHERE version = ? AND committed = 1",
                (self.plan.target_version,),
            ).fetchone()
            if existing_step is not None or target_binding_row is not None:
                self._set_round_watermark(connection, now)
                return {
                    "ok": True,
                    "reused": True,
                    "at_ms": now,
                    "round_id": self.plan.round_id,
                    "target_version": self.plan.target_version,
                }
            if round_state["status"] not in {"prepared", "patched"}:
                fail(
                    ErrorCode.CONFLICT,
                    f"轮次状态 {round_state['status']} 不允许提交补丁",
                    round_id=self.plan.round_id,
                    actual=round_state["status"],
                )
            if self.plan.patch_id != self.patch.patch_id:
                fail(ErrorCode.PATCH_INVALID, "轮次补丁 ID 不一致")

            composed = compose_patch_shards(
                manifest=self.manifest,
                patch=self.patch,
                samples_root=self.samples.root,
            )
            comparisons = compare_target_directories(
                materializer=self.materializer,
                manifest=self.manifest,
                target_version=self.plan.target_version,
                composed=composed,
            )
            materialized = 0
            for item in composed:
                self.materializer.materialize(
                    connection,
                    digest=item.digest,
                    source_path=item.source_path,
                    at_ms=now,
                    version=self.plan.target_version,
                    layer=item.layer,
                    shard_index=item.shard_index,
                )
                materialized += 1
                if materialize_callback is not None:
                    materialize_callback(materialized)
                if crash_after_materialize is not None and materialized >= crash_after_materialize:
                    os._exit(70)
            target = self.manifest.model(self.plan.target_version)
            for layer in target.layers:
                for shard in layer.shards:
                    connection.execute(
                        "INSERT OR REPLACE INTO shards(version, layer, shard_index, byte_start, "
                        "byte_end, digest, declared_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            target.version,
                            layer.name,
                            shard.index,
                            shard.byte_start,
                            shard.byte_end,
                            shard.digest,
                            shard.path,
                        ),
                    )
            binding = self.manifest.version_binding(target.version)
            connection.execute(
                "INSERT OR REPLACE INTO version_bindings(version, binding_json, binding_digest, "
                "committed, created_at_ms) VALUES (?, ?, ?, 1, ?)",
                (target.version, canonical_json(binding), binding["binding_digest"], now),
            )
            report = analyze_invalidation(
                self.manifest,
                baseline_version=self.plan.baseline_version,
                target_version=self.plan.target_version,
                target_replica_ids=self.plan.replica_ids,
                all_replica_ids=[replica.replica_id for replica in self.replicas],
                at_ms=now,
            )
            self._insert_decisions(connection, report)
            self._record_step(
                connection,
                step_key="patch.applied",
                at_ms=now,
                payload={
                    "patch_id": self.patch.patch_id,
                    "target_version": self.plan.target_version,
                    "binding_digest": binding["binding_digest"],
                    "materialized_shards": materialized,
                    "changed_shards": [
                        {
                            "layer": item.layer,
                            "shard_index": item.shard_index,
                            "digest": item.digest,
                            "source": item.source,
                        }
                        for item in composed
                        if item.source == "patch"
                    ],
                },
            )
            self._set_round_watermark(connection, now, status="patched")
            self._event(
                connection,
                at_ms=now,
                event_type="patch_committed",
                payload={
                    "patch_id": self.patch.patch_id,
                    "target_version": self.plan.target_version,
                    "binding_digest": binding["binding_digest"],
                    "affected_replicas": report.affected_replicas,
                },
            )
            return {
                "ok": True,
                "reused": False,
                "at_ms": now,
                "round_id": self.plan.round_id,
                "target_version": self.plan.target_version,
                "binding_digest": binding["binding_digest"],
                "shard_chain": binding["shard_chain"],
                "comparison_count": len(comparisons),
                "comparisons": comparisons,
                "invalidations": report.as_dict(),
            }

    def start_warmup(self, *, at_ms: int) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            round_state = self._round(connection)
            if round_state["status"] in ROUND_FINAL_STATES | ROUND_ROLLBACK_STATES:
                fail(
                    ErrorCode.CONFLICT,
                    f"轮次状态 {round_state['status']} 不允许开始预热",
                    round_id=self.plan.round_id,
                    actual=round_state["status"],
                )
            existing = self._step(connection, "warmup.started")
            if existing is not None:
                self._set_round_watermark(connection, now)
                return {
                    "ok": True,
                    "reused": True,
                    "at_ms": now,
                    "round_id": self.plan.round_id,
                    "replicas": self._round_replicas(connection),
                }
            if self._step(connection, "patch.applied") is None:
                fail(
                    ErrorCode.TARGET_NOT_READY,
                    "补丁尚未提交，不能开始预热",
                    target_version=self.plan.target_version,
                )
            issues = self._target_not_ready(connection, verify_content=True)
            if issues:
                fail(
                    ErrorCode.TARGET_NOT_READY,
                    "目标版本尚未就绪，不能开始预热",
                    target_version=self.plan.target_version,
                    not_ready=issues,
                )
            for replica in self._round_replicas(connection):
                if replica["state"] == "pending":
                    self._transition_replica(
                        connection,
                        replica_id=replica["replica_id"],
                        new_state="loading",
                        at_ms=now,
                        warmup_started_at_ms=now,
                        warmup_expected_ms=now + self.plan.warmup_duration_ms,
                        warmup_deadline_ms=now + self.plan.warmup_timeout_ms,
                    )
            self._record_step(
                connection,
                step_key="warmup.started",
                at_ms=now,
                payload={
                    "duration_ms": self.plan.warmup_duration_ms,
                    "timeout_ms": self.plan.warmup_timeout_ms,
                },
            )
            self._set_round_watermark(connection, now, status="warming")
            self._event(
                connection,
                at_ms=now,
                event_type="warmup_started",
                payload={
                    "replica_ids": list(self.plan.replica_ids),
                    "expected_complete_ms": now + self.plan.warmup_duration_ms,
                    "deadline_ms": now + self.plan.warmup_timeout_ms,
                },
            )
            return {
                "ok": True,
                "reused": False,
                "at_ms": now,
                "round_id": self.plan.round_id,
                "status": "warming",
                "replicas": self._round_replicas(connection),
            }

    def _transition_replica(
        self,
        connection: sqlite3.Connection,
        *,
        replica_id: str,
        new_state: str,
        at_ms: int,
        warmup_started_at_ms: int | None = None,
        warmup_expected_ms: int | None = None,
        warmup_deadline_ms: int | None = None,
        warmup_completed_at_ms: int | None = None,
        serving_version: int | None = None,
        gray_ratio: float | None = None,
        failure_reason: str | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM round_replicas WHERE round_id = ? AND replica_id = ?",
            (self.plan.round_id, replica_id),
        ).fetchone()
        if row is None:
            fail(
                ErrorCode.NOT_FOUND,
                f"轮次副本 {replica_id} 不存在",
                replica_id=replica_id,
                round_id=self.plan.round_id,
            )
        current = row["state"]
        if new_state not in TRANSITIONS.get(current, set()) and new_state != current:
            fail(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"副本 {replica_id} 不能从 {current} 迁移到 {new_state}",
                replica_id=replica_id,
                actual=current,
                expected=sorted(TRANSITIONS.get(current, set())),
            )
        updates: dict[str, Any] = {
            "state": new_state,
            "updated_at_ms": at_ms,
        }
        for key, value in (
            ("warmup_started_at_ms", warmup_started_at_ms),
            ("warmup_expected_ms", warmup_expected_ms),
            ("warmup_deadline_ms", warmup_deadline_ms),
            ("warmup_completed_at_ms", warmup_completed_at_ms),
            ("serving_version", serving_version),
            ("gray_ratio", gray_ratio),
            ("failure_reason", failure_reason),
        ):
            if value is not None:
                updates[key] = value
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE round_replicas SET {assignments} WHERE round_id = ? AND replica_id = ?",
            (*updates.values(), self.plan.round_id, replica_id),
        )

    def _trigger_rollback_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
        reason: str,
        replica_id: str | None = None,
    ) -> dict[str, Any]:
        round_state = self._round(connection)
        if round_state["status"] in ROUND_FINAL_STATES:
            return {"ok": True, "reused": True, "status": round_state["status"]}
        if round_state["status"] == "rolling_back":
            return {"ok": True, "reused": True, "status": "rolling_back"}
        initial_level = int(round_state["current_level"])
        deadline = at_ms + self.plan.rollback_window_ms
        now = at_ms
        connection.execute(
            "UPDATE rounds SET status = 'rolling_back', failure_at_ms = ?, "
            "rollback_deadline_ms = ?, rollback_reason = ? WHERE round_id = ?",
            (at_ms, deadline, reason, self.plan.round_id),
        )
        for replica in self._round_replicas(connection):
            if replica["state"] not in {"rolling_back", "rolled_back", "rollback_not_achieved"}:
                self._transition_replica(
                    connection,
                    replica_id=replica["replica_id"],
                    new_state="rolling_back",
                    at_ms=now,
                    failure_reason=reason,
                )
        payload = {
            "reason": reason,
            "initial_level": initial_level,
            "failure_at_ms": at_ms,
            "deadline_ms": deadline,
            "replica_id": replica_id,
        }
        self._record_step(
            connection,
            step_key="rollback.started",
            at_ms=now,
            payload=payload,
        )
        self._set_round_watermark(connection, now)
        self._event(
            connection,
            at_ms=now,
            event_type="rollback_started",
            payload=payload,
        )
        return {
            "ok": True,
            "reused": False,
            "status": "rolling_back",
            "failure_at_ms": at_ms,
            "deadline_ms": deadline,
            "initial_level": initial_level,
        }

    def trigger_rollback(
        self,
        *,
        at_ms: int,
        reason: str,
        replica_id: str | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            result = self._trigger_rollback_in_tx(
                connection,
                at_ms=now,
                reason=reason,
                replica_id=replica_id,
            )
            self._set_round_watermark(connection, now)
            return {"at_ms": now, "round_id": self.plan.round_id, **result}

    def _process_rollback(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
    ) -> dict[str, Any]:
        round_state = self._round(connection)
        if round_state["status"] in ROUND_FINAL_STATES:
            return {
                "ok": True,
                "status": round_state["status"],
                "current_level": round_state["current_level"],
            }
        if round_state["status"] != "rolling_back":
            return {"ok": True, "status": round_state["status"], "skipped": True}
        started = self._step(connection, "rollback.started")
        payload = _loads(started["payload_json"], {}) if started else {}
        failure_at = int(round_state["failure_at_ms"])
        deadline = int(round_state["rollback_deadline_ms"])
        initial_level = int(payload.get("initial_level", round_state["current_level"]))
        if at_ms > deadline:
            for replica in self._round_replicas(connection):
                if replica["state"] == "rolling_back":
                    self._transition_replica(
                        connection,
                        replica_id=replica["replica_id"],
                        new_state="rollback_not_achieved",
                        at_ms=at_ms,
                        serving_version=self.plan.baseline_version,
                        gray_ratio=0.0,
                    )
            connection.execute(
                "UPDATE rounds SET status = 'rollback_not_achieved', current_level = 0, "
                "current_ratio = 0.0 WHERE round_id = ?",
                (self.plan.round_id,),
            )
            self._record_step(
                connection,
                step_key="rollback.missed",
                at_ms=at_ms,
                payload={"deadline_ms": deadline, "at_ms": at_ms},
            )
            self._diagnostic(
                connection,
                at_ms=at_ms,
                code=ErrorCode.ROLLBACK_NOT_ACHIEVED,
                message="回退窗口耗尽，未能在窗口内恢复基线版本",
                details={
                    "round_id": self.plan.round_id,
                    "expected": deadline,
                    "actual": at_ms,
                },
            )
            self._event(
                connection,
                at_ms=at_ms,
                event_type="rollback_not_achieved",
                payload={"deadline_ms": deadline, "at_ms": at_ms},
            )
            self._set_round_watermark(connection, at_ms)
            return {
                "ok": True,
                "status": "rollback_not_achieved",
                "current_level": 0,
                "deadline_ms": deadline,
            }
        elapsed = max(0, at_ms - failure_at)
        due_steps = elapsed // self.plan.rollback_step_ms
        target_level = max(0, initial_level - int(due_steps))
        current_level = int(round_state["current_level"])
        while current_level > target_level:
            current_level -= 1
            ratio = 0.0 if current_level == 0 else self.plan.stage(current_level).ratio
            connection.execute(
                "UPDATE rounds SET current_level = ?, current_ratio = ? WHERE round_id = ?",
                (current_level, ratio, self.plan.round_id),
            )
            self._record_step(
                connection,
                step_key=f"rollback.level.{current_level}",
                at_ms=at_ms,
                payload={"level": current_level, "ratio": ratio},
            )
            self._event(
                connection,
                at_ms=at_ms,
                event_type="rollback_step",
                payload={"level": current_level, "ratio": ratio},
            )
        if current_level == 0:
            for replica in self._round_replicas(connection):
                if replica["state"] == "rolling_back":
                    self._transition_replica(
                        connection,
                        replica_id=replica["replica_id"],
                        new_state="rolled_back",
                        at_ms=at_ms,
                        serving_version=self.plan.baseline_version,
                        gray_ratio=0.0,
                    )
            connection.execute(
                "UPDATE rounds SET status = 'rolled_back', current_level = 0, "
                "current_ratio = 0.0 WHERE round_id = ?",
                (self.plan.round_id,),
            )
            if self._step(connection, "rollback.restored") is None:
                self._record_step(
                    connection,
                    step_key="rollback.restored",
                    at_ms=at_ms,
                    payload={"version": self.plan.baseline_version},
                )
                self._event(
                    connection,
                    at_ms=at_ms,
                    event_type="baseline_restored",
                    payload={"version": self.plan.baseline_version},
                )
            self._set_round_watermark(connection, at_ms)
            return {
                "ok": True,
                "status": "rolled_back",
                "current_level": 0,
                "restored_version": self.plan.baseline_version,
                "within_window": True,
            }
        self._set_round_watermark(connection, at_ms)
        return {
            "ok": True,
            "status": "rolling_back",
            "current_level": current_level,
            "deadline_ms": deadline,
        }

    def _advance_stage_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
        level: int,
    ) -> dict[str, Any]:
        stage = self.plan.stage(level)
        for replica in self._round_replicas(connection):
            if replica["state"] == "prewarmed":
                self._transition_replica(
                    connection,
                    replica_id=replica["replica_id"],
                    new_state="gray",
                    at_ms=at_ms,
                )
            connection.execute(
                "UPDATE round_replicas SET serving_version = ?, gray_ratio = ?, updated_at_ms = ? "
                "WHERE round_id = ? AND replica_id = ?",
                (
                    self.plan.target_version,
                    stage.ratio,
                    at_ms,
                    self.plan.round_id,
                    replica["replica_id"],
                ),
            )
        status = "released" if level == self.plan.max_level else "gray"
        connection.execute(
            "UPDATE rounds SET status = ?, current_level = ?, current_ratio = ? "
            "WHERE round_id = ?",
            (status, level, stage.ratio, self.plan.round_id),
        )
        self._record_step(
            connection,
            step_key=f"stage.{level}",
            at_ms=at_ms,
            payload={"level": level, "ratio": stage.ratio},
        )
        self._set_round_watermark(connection, at_ms)
        self._event(
            connection,
            at_ms=at_ms,
            event_type="stage_advanced",
            payload={"level": level, "ratio": stage.ratio, "status": status},
        )
        return {
            "ok": True,
            "advanced": True,
            "reused": False,
            "at_ms": at_ms,
            "level": level,
            "ratio": stage.ratio,
            "status": status,
        }

    def advance_stage(self, *, at_ms: int, level: int | None = None) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            round_state = self._round(connection)
            if round_state["status"] in ROUND_FINAL_STATES | ROUND_ROLLBACK_STATES:
                fail(
                    ErrorCode.CONFLICT,
                    f"轮次状态 {round_state['status']} 不允许灰度推进",
                    actual=round_state["status"],
                )
            current_level = int(round_state["current_level"])
            if level is None and current_level >= self.plan.max_level:
                return {
                    "ok": True,
                    "advanced": False,
                    "reused": True,
                    "at_ms": now,
                    "level": current_level,
                    "ratio": float(round_state["current_ratio"]),
                    "status": round_state["status"],
                }
            target_level = (
                current_level + 1
                if level is None
                else int_field(level, "level", minimum=1)
            )
            if target_level < current_level:
                fail(
                    ErrorCode.CONFLICT,
                    f"档位 {target_level} 已越过当前档位 {current_level}",
                    actual=target_level,
                    expected=current_level,
                )
            if target_level == current_level:
                step = self._step(connection, f"stage.{target_level}")
                if step is not None:
                    return {
                        "ok": True,
                        "advanced": False,
                        "reused": True,
                        "at_ms": now,
                        "level": target_level,
                        "ratio": self.plan.stage(target_level).ratio,
                        "status": round_state["status"],
                    }
                fail(
                    ErrorCode.CONFLICT,
                    f"当前档位 {current_level} 缺少已提交步骤记录",
                    actual=target_level,
                    expected=f"stage.{target_level}",
                )
            if target_level != current_level + 1:
                fail(
                    ErrorCode.CONFLICT,
                    "灰度档位必须逐档推进",
                    actual=target_level,
                    expected=current_level + 1,
                )
            stage = self.plan.stage(target_level)
            if now < stage.at_ms:
                fail(
                    ErrorCode.INVALID_CLOCK,
                    f"逻辑时钟尚未到达档位 {target_level} 的计划时间",
                    expected=stage.at_ms,
                    actual=now,
                )
            required_states = {"prewarmed", "gray"} if target_level == 1 else {"gray"}
            issues = self._target_not_ready(
                connection,
                verify_content=True,
                include_replicas=True,
                required_replica_states=required_states,
            )
            if issues:
                payload = {
                    "level": target_level,
                    "current_level": current_level,
                    "not_ready": issues,
                }
                self._record_step(
                    connection,
                    step_key=f"stage_blocked.{target_level}",
                    at_ms=now,
                    payload=payload,
                    status="blocked",
                )
                self._event(
                    connection,
                    at_ms=now,
                    event_type="stage_blocked",
                    payload=payload,
                )
                self._diagnostic(
                    connection,
                    at_ms=now,
                    code=ErrorCode.TARGET_NOT_READY,
                    message="灰度档位推进被未就绪项阻断",
                    details=payload,
                )
                self._set_round_watermark(connection, now)
                return {
                    "ok": True,
                    "advanced": False,
                    "reused": False,
                    "at_ms": now,
                    "level": current_level,
                    "requested_level": target_level,
                    "status": round_state["status"],
                    "not_ready": issues,
                }
            return self._advance_stage_in_tx(connection, at_ms=now, level=target_level)

    def _auto_advance_due(
        self,
        connection: sqlite3.Connection,
        *,
        at_ms: int,
    ) -> dict[str, Any] | None:
        while True:
            round_state = self._round(connection)
            if round_state["status"] in ROUND_FINAL_STATES | ROUND_ROLLBACK_STATES:
                return None
            current_level = int(round_state["current_level"])
            if current_level >= self.plan.max_level:
                return None
            next_level = current_level + 1
            stage = self.plan.stage(next_level)
            if at_ms < stage.at_ms:
                return None
            required_states = {"prewarmed", "gray"} if next_level == 1 else {"gray"}
            issues = self._target_not_ready(
                connection,
                verify_content=True,
                include_replicas=True,
                required_replica_states=required_states,
            )
            if issues:
                payload = {
                    "level": next_level,
                    "current_level": current_level,
                    "not_ready": issues,
                }
                if self._step(connection, f"stage_blocked.{next_level}") is None:
                    self._record_step(
                        connection,
                        step_key=f"stage_blocked.{next_level}",
                        at_ms=at_ms,
                        payload=payload,
                        status="blocked",
                    )
                    self._event(
                        connection,
                        at_ms=at_ms,
                        event_type="stage_blocked",
                        payload=payload,
                    )
                    self._diagnostic(
                        connection,
                        at_ms=at_ms,
                        code=ErrorCode.TARGET_NOT_READY,
                        message="灰度档位自动推进被未就绪项阻断",
                        details=payload,
                    )
                self._set_round_watermark(connection, at_ms)
                return {
                    "ok": True,
                    "advanced": False,
                    "level": current_level,
                    "requested_level": next_level,
                    "not_ready": issues,
                }
            result = self._advance_stage_in_tx(connection, at_ms=at_ms, level=next_level)
            if next_level >= self.plan.max_level:
                return result

    def tick(self, *, at_ms: int) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            round_state = self._round(connection)
            warmup_completed: list[str] = []
            warmup_failures: list[dict[str, Any]] = []
            if round_state["status"] == "warming":
                target_issues: list[dict[str, Any]] | None = None
                for replica in self._round_replicas(connection):
                    if replica["state"] != "loading":
                        continue
                    deadline = int(replica["warmup_deadline_ms"])
                    expected = int(replica["warmup_expected_ms"])
                    if now > deadline:
                        warmup_failures.append(
                            {
                                "replica_id": replica["replica_id"],
                                "type": "warmup_timeout",
                                "deadline_ms": deadline,
                            }
                        )
                        continue
                    if now < expected:
                        continue
                    if target_issues is None:
                        target_issues = self._target_not_ready(connection, verify_content=True)
                    if target_issues:
                        warmup_failures.append(
                            {
                                "replica_id": replica["replica_id"],
                                "type": "target_not_ready",
                                "not_ready": target_issues,
                            }
                        )
                        continue
                    self._transition_replica(
                        connection,
                        replica_id=replica["replica_id"],
                        new_state="prewarmed",
                        at_ms=now,
                        warmup_completed_at_ms=now,
                    )
                    warmup_completed.append(replica["replica_id"])
                if warmup_failures:
                    reason = "; ".join(
                        f"{item['replica_id']}:{item['type']}" for item in warmup_failures
                    )
                    self._event(
                        connection,
                        at_ms=now,
                        event_type="warmup_failed",
                        payload={"failures": warmup_failures},
                    )
                    for failure in warmup_failures:
                        failure_code = (
                            ErrorCode.SHARD_DIGEST_MISMATCH
                            if failure.get("type") == "target_not_ready"
                            else ErrorCode.TARGET_NOT_READY
                        )
                        self._diagnostic(
                            connection,
                            at_ms=now,
                            code=failure_code,
                            message=f"副本 {failure['replica_id']} 预热失败: {failure['type']}",
                            details=failure,
                        )
                    self._trigger_rollback_in_tx(
                        connection,
                        at_ms=now,
                        reason=reason,
                        replica_id=warmup_failures[0]["replica_id"],
                    )
                    round_state = self._round(connection)
                else:
                    replicas_now = self._round_replicas(connection)
                    if all(item["state"] in {"prewarmed", "gray"} for item in replicas_now):
                        new_status = "ready" if int(round_state["current_level"]) == 0 else round_state["status"]
                        connection.execute(
                            "UPDATE rounds SET status = ? WHERE round_id = ?",
                            (new_status, self.plan.round_id),
                        )
                        round_state = self._round(connection)
                        if warmup_completed:
                            self._event(
                                connection,
                                at_ms=now,
                                event_type="warmup_completed",
                                payload={"replica_ids": sorted(warmup_completed)},
                            )
            round_state = self._round(connection)
            if round_state["status"] in ROUND_GRAY_STATES:
                auto_result = self._auto_advance_due(connection, at_ms=now)
            else:
                auto_result = None
            round_state = self._round(connection)
            rollback_result = None
            if round_state["status"] == "rolling_back":
                rollback_result = self._process_rollback(connection, at_ms=now)
            self._set_round_watermark(connection, now)
            return {
                "ok": True,
                "at_ms": now,
                "round_id": self.plan.round_id,
                "warmup_completed": sorted(warmup_completed),
                "warmup_failures": warmup_failures,
                "auto_stage": auto_result,
                "rollback": rollback_result,
                "round": self._round(connection),
                "replicas": self._round_replicas(connection),
            }

    def _binding_row(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM version_bindings WHERE version = ? AND committed = 1",
            (version,),
        ).fetchone()
        if row is None:
            fail(
                ErrorCode.TARGET_NOT_READY,
                f"模型版本 {version} 尚未形成完整已提交版本组合",
                version=version,
            )
        binding = _loads(row["binding_json"], {})
        binding["binding_digest"] = row["binding_digest"]
        return binding

    def begin_request(
        self,
        *,
        replica_id: str,
        request_id: str,
        input_payload: Any,
        at_ms: int,
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            replica_row = connection.execute(
                "SELECT * FROM replicas WHERE replica_id = ?",
                (replica_id,),
            ).fetchone()
            if replica_row is None:
                fail(
                    ErrorCode.NOT_FOUND,
                    f"副本 {replica_id} 不存在",
                    replica_id=replica_id,
                )
            input_json = canonical_json(input_payload)
            existing = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["replica_id"] != replica_id or existing["input_json"] != input_json:
                    fail(
                        ErrorCode.CONFLICT,
                        "相同 request_id 使用了不同的副本或输入",
                        request_id=request_id,
                        replica_id=replica_id,
                    )
                binding = _loads(existing["binding_json"], {})
                self._set_round_watermark(connection, now)
                return {
                    "ok": True,
                    "reused": True,
                    "at_ms": now,
                    "request_id": request_id,
                    "replica_id": replica_id,
                    "lane": "target" if existing["version"] == self.plan.target_version else "baseline",
                    "version": existing["version"],
                    "level": existing["level"],
                    "binding": binding_stable_view(binding),
                    "status": existing["status"],
                }
            round_state = self._round(connection)
            round_replica = connection.execute(
                "SELECT * FROM round_replicas WHERE round_id = ? AND replica_id = ?",
                (self.plan.round_id, replica_id),
            ).fetchone()
            eligible = bool(replica_row["eligible"]) and round_replica is not None
            in_rollback = round_state["status"] in ROUND_ROLLBACK_STATES | ROUND_FINAL_STATES
            target_eligible = (
                eligible
                and not in_rollback
                and round_state["status"] in ROUND_GRAY_STATES
                and int(round_state["current_level"]) > 0
                and round_replica is not None
                and round_replica["state"] == "gray"
            )
            lane = "baseline"
            if target_eligible:
                bucket_hash = int(sha256_text(request_id).split(":", 1)[1][:16], 16)
                bucket = bucket_hash / float(16**16)
                if bucket < float(round_state["current_ratio"]):
                    lane = "target"
            if lane == "target":
                issues = self._target_not_ready(connection, verify_content=True)
                if issues:
                    fail(
                        ErrorCode.TARGET_NOT_READY,
                        "请求无法绑定目标版本：版本组合未完整就绪",
                        target_version=self.plan.target_version,
                        not_ready=issues,
                        request_id=request_id,
                    )
                selected_version = self.plan.target_version
            else:
                selected_version = int(replica_row["loaded_version"])
            binding = self._binding_row(connection, selected_version)
            level = int(round_state["current_level"]) if lane == "target" else 0
            connection.execute(
                "INSERT INTO requests(request_id, replica_id, round_id, version, level, "
                "binding_json, binding_digest, input_json, status, started_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)",
                (
                    request_id,
                    replica_id,
                    self.plan.round_id,
                    selected_version,
                    level,
                    canonical_json(binding),
                    binding["binding_digest"],
                    input_json,
                    now,
                ),
            )
            self._event(
                connection,
                at_ms=now,
                event_type="request_started",
                payload={
                    "request_id": request_id,
                    "replica_id": replica_id,
                    "lane": lane,
                    "version": selected_version,
                    "binding_digest": binding["binding_digest"],
                    "level": level,
                },
            )
            self._set_round_watermark(connection, now)
            return {
                "ok": True,
                "reused": False,
                "at_ms": now,
                "request_id": request_id,
                "replica_id": replica_id,
                "lane": lane,
                "version": selected_version,
                "level": level,
                "binding": binding_stable_view(binding),
                "status": "in_progress",
            }

    def complete_request(self, *, request_id: str, at_ms: int) -> dict[str, Any]:
        with self.store.transaction() as connection:
            now = self._clock(connection, at_ms)
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                fail(ErrorCode.NOT_FOUND, f"请求 {request_id} 不存在", request_id=request_id)
            if row["status"] == "completed":
                result = _loads(row["result_json"], {})
                self._set_round_watermark(connection, now)
                return {
                    "ok": True,
                    "reused": True,
                    "at_ms": now,
                    "request_id": request_id,
                    "replica_id": row["replica_id"],
                    "version": row["version"],
                    "level": row["level"],
                    "result": result,
                    "binding_digest": row["binding_digest"],
                }
            binding = _loads(row["binding_json"], {})
            input_payload = _loads(row["input_json"], [])
            result = compute_inference(
                blob_dir=self.materializer.blob_dir,
                binding=binding,
                payload=input_payload,
            )
            connection.execute(
                "UPDATE requests SET status = 'completed', result_json = ?, "
                "result_bytes_hex = ?, result_digest = ?, completed_at_ms = ? "
                "WHERE request_id = ?",
                (
                    canonical_json(result),
                    result["output_bits"],
                    result["result_digest"],
                    now,
                    request_id,
                ),
            )
            self._event(
                connection,
                at_ms=now,
                event_type="request_completed",
                payload={
                    "request_id": request_id,
                    "replica_id": row["replica_id"],
                    "version": row["version"],
                    "result_digest": result["result_digest"],
                    "binding_digest": row["binding_digest"],
                },
            )
            self._set_round_watermark(connection, now)
            return {
                "ok": True,
                "reused": False,
                "at_ms": now,
                "request_id": request_id,
                "replica_id": row["replica_id"],
                "version": row["version"],
                "level": row["level"],
                "result": result,
                "binding_digest": row["binding_digest"],
            }

    def resume(self, *, at_ms: int | None = None) -> dict[str, Any]:
        with self.store.read() as connection:
            round_state = self._round(connection)
            clock_row = connection.execute(
                "SELECT watermark_ms FROM clock_state WHERE id = 1"
            ).fetchone()
        persisted = max(
            int(round_state["logical_watermark_ms"]),
            int(clock_row["watermark_ms"]),
        )
        logical_ms = persisted if at_ms is None else at_ms
        if logical_ms == 0:
            logical_ms = self.plan.start_at_ms
        recovered: list[dict[str, Any]] = []
        round_state = self._read_round()
        if round_state["status"] == "prepared":
            recovered.append(self.apply_patch(at_ms=logical_ms))
            round_state = self._read_round()
        if round_state["status"] == "patched":
            recovered.append(self.start_warmup(at_ms=logical_ms))
            round_state = self._read_round()
        if round_state["status"] in {"warming", "ready", "gray", "released", "rolling_back"}:
            recovered.append(self.tick(at_ms=logical_ms))
        return {
            "ok": True,
            "at_ms": logical_ms,
            "recovered_steps": recovered,
            "status": self._read_round()["status"],
        }

    def _read_round(self) -> dict[str, Any]:
        with self.store.read() as connection:
            return self._round(connection)

    def _binding_view_for_version(
        self,
        connection: sqlite3.Connection,
        version: int,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM version_bindings WHERE version = ? AND committed = 1",
            (version,),
        ).fetchone()
        if row is None:
            return None
        binding = _loads(row["binding_json"], {})
        binding["binding_digest"] = row["binding_digest"]
        return binding_stable_view(binding)

    def snapshot(self) -> dict[str, Any]:
        with self.store.read() as connection:
            round_row = self._round(connection)
            clock_row = connection.execute(
                "SELECT watermark_ms, tolerance_ms, last_requested_ms FROM clock_state WHERE id = 1"
            ).fetchone()
            replicas_rows = rows_to_dicts(
                connection.execute("SELECT * FROM replicas ORDER BY replica_id").fetchall()
            )
            round_replicas = {
                item["replica_id"]: item for item in self._round_replicas(connection)
            }
            invalidations = rows_to_dicts(
                connection.execute(
                    "SELECT * FROM invalidations WHERE round_id = ? "
                    "ORDER BY replica_id, version, cache_type, decision, trigger, id",
                    (self.plan.round_id,),
                ).fetchall()
            )
            for item in invalidations:
                item["path"] = _loads(item.pop("path_json"), [])
                item["source"] = _loads(item.pop("source_json"), {})
            invalidations_by_replica: dict[str, list[dict[str, Any]]] = {}
            for item in invalidations:
                invalidations_by_replica.setdefault(item["replica_id"], []).append(item)
            binding_rows = rows_to_dicts(
                connection.execute(
                    "SELECT * FROM version_bindings WHERE committed = 1 ORDER BY version"
                ).fetchall()
            )
            bindings: dict[int, dict[str, Any]] = {}
            for row in binding_rows:
                binding = _loads(row["binding_json"], {})
                binding["binding_digest"] = row["binding_digest"]
                bindings[int(row["version"])] = binding
            shard_rows = rows_to_dicts(
                connection.execute(
                    "SELECT version, layer, shard_index, byte_start, byte_end, digest "
                    "FROM shards ORDER BY version, layer, shard_index"
                ).fetchall()
            )
            shard_chains: dict[str, list[dict[str, Any]]] = {}
            for shard in shard_rows:
                shard_chains.setdefault(str(shard["version"]), []).append(shard)
            replicas_status: list[dict[str, Any]] = []
            for replica in replicas_rows:
                replica_id = replica["replica_id"]
                rr = round_replicas.get(replica_id)
                serving_version = (
                    int(rr["serving_version"]) if rr is not None else int(replica["loaded_version"])
                )
                binding_view = self._binding_view_for_version(connection, serving_version)
                decisions = invalidations_by_replica.get(replica_id, [])
                reasons = [
                    {
                        "cache_type": item["cache_type"],
                        "version": item["version"],
                        "decision": item["decision"],
                        "trigger": item["trigger"],
                        "path": item["path"],
                        "source": item["source"],
                    }
                    for item in decisions
                    if item["decision"] == "invalidated"
                ]
                reusable = [
                    {
                        "cache_type": item["cache_type"],
                        "version": item["version"],
                        "trigger": item["trigger"],
                        "path": item["path"],
                    }
                    for item in decisions
                    if item["decision"] == "reused"
                ]
                replicas_status.append(
                    {
                        "replica_id": replica_id,
                        "eligible": bool(replica["eligible"]),
                        "baseline_version": int(replica["loaded_version"]),
                        "baseline_state": replica["baseline_state"],
                        "baseline_ratio": float(replica["baseline_ratio"]),
                        "state": rr["state"] if rr is not None else replica["baseline_state"],
                        "serving_version": serving_version,
                        "stage_version": int(rr["stage_version"]) if rr is not None else None,
                        "gray_ratio": float(rr["gray_ratio"]) if rr is not None else 0.0,
                        "warmup_started_at_ms": rr["warmup_started_at_ms"] if rr is not None else None,
                        "warmup_expected_ms": rr["warmup_expected_ms"] if rr is not None else None,
                        "warmup_deadline_ms": rr["warmup_deadline_ms"] if rr is not None else None,
                        "warmup_completed_at_ms": rr["warmup_completed_at_ms"] if rr is not None else None,
                        "failure_reason": rr["failure_reason"] if rr is not None else None,
                        "version_binding": binding_view,
                        "shard_chain": (
                            binding_view.get("shard_chain", []) if binding_view is not None else []
                        ),
                        "invalidated": reasons,
                        "reusable": reusable,
                    }
                )
            requests = rows_to_dicts(
                connection.execute(
                    "SELECT request_id, replica_id, round_id, version, level, binding_digest, "
                    "status, result_json, result_digest, started_at_ms, completed_at_ms "
                    "FROM requests ORDER BY replica_id, started_at_ms, request_id"
                ).fetchall()
            )
            for item in requests:
                item["result"] = _loads(item.get("result_json"), None)
                item.pop("result_json", None)
            events = []
            for row in connection.execute(
                "SELECT * FROM events WHERE round_id = ? ORDER BY at_ms, id",
                (self.plan.round_id,),
            ).fetchall():
                item = row_to_dict(row) or {}
                item["payload"] = _loads(item.pop("payload_json"), {})
                events.append(item)
            diagnostics = []
            for row in connection.execute(
                "SELECT * FROM diagnostics WHERE round_id = ? ORDER BY id",
                (self.plan.round_id,),
            ).fetchall():
                item = row_to_dict(row) or {}
                item["details"] = _loads(item.pop("details_json"), {})
                diagnostics.append(item)
            round_status = str(round_row["status"])
            include_replica_readiness = round_status not in (
                ROUND_FINAL_STATES | ROUND_ROLLBACK_STATES
            )
            required_states = (
                {"prewarmed", "gray"}
                if int(round_row["current_level"]) == 0
                else {"gray"}
            )
            not_ready = self._target_not_ready(
                connection,
                verify_content=False,
                include_replicas=include_replica_readiness,
                required_replica_states=required_states,
            )
            materialization_rows = rows_to_dicts(
                connection.execute(
                    "SELECT digest, path, size, created_at_ms FROM materializations ORDER BY digest"
                ).fetchall()
            )
            return {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "logical_clock": {
                    "watermark_ms": int(clock_row["watermark_ms"]),
                    "tolerance_ms": int(clock_row["tolerance_ms"]),
                    "last_requested_ms": int(clock_row["last_requested_ms"]),
                },
                "round": {
                    "round_id": round_row["round_id"],
                    "status": round_row["status"],
                    "baseline_version": int(round_row["baseline_version"]),
                    "target_version": int(round_row["target_version"]),
                    "current_level": int(round_row["current_level"]),
                    "current_ratio": float(round_row["current_ratio"]),
                    "failure_at_ms": round_row["failure_at_ms"],
                    "rollback_deadline_ms": round_row["rollback_deadline_ms"],
                    "rollback_reason": round_row["rollback_reason"],
                    "logical_watermark_ms": int(round_row["logical_watermark_ms"]),
                    "stages": [stage.as_dict() for stage in self.plan.stages],
                    "replica_ids": list(self.plan.replica_ids),
                },
                "not_ready": not_ready,
                "replicas": replicas_status,
                "shard_chains": shard_chains,
                "version_bindings": {
                    str(version): binding_stable_view(binding)
                    for version, binding in sorted(bindings.items())
                },
                "requests": requests,
                "invalidations": invalidations,
                "events": events,
                "diagnostics": diagnostics,
                "materializations": {
                    "count": len(materialization_rows),
                    "items": materialization_rows,
                },
                "stable_order": {
                    "replicas": "replica_id",
                    "shards": "version,layer,shard_index",
                    "events": "at_ms,id",
                    "requests": "replica_id,started_at_ms,request_id",
                },
            }

    def execute_clock_script(self, scenario: str = "normal") -> dict[str, Any]:
        actions = self.samples.clock_actions(scenario)
        results: list[dict[str, Any]] = []
        for action in actions:
            command = action.command
            args = dict(action.args)
            if command == "patch":
                result = self.apply_patch(
                    at_ms=action.at_ms,
                    crash_after_materialize=args.get("crash_after_materialize"),
                )
            elif command == "warmup":
                result = self.start_warmup(at_ms=action.at_ms)
            elif command == "tick":
                result = self.tick(at_ms=action.at_ms)
            elif command == "stage":
                result = self.advance_stage(
                    at_ms=action.at_ms,
                    level=args.get("level"),
                )
            elif command == "request":
                result = self.begin_request(
                    replica_id=args["replica_id"],
                    request_id=args["request_id"],
                    input_payload=args.get("input", [1.0, 0.5, -0.25, 0.125]),
                    at_ms=action.at_ms,
                )
            elif command == "complete":
                result = self.complete_request(
                    request_id=args["request_id"],
                    at_ms=action.at_ms,
                )
            elif command == "abort":
                result = self.trigger_rollback(
                    at_ms=action.at_ms,
                    reason=args.get("reason", "manual_abort"),
                    replica_id=args.get("replica_id"),
                )
            elif command == "status":
                result = {"ok": True, "status": self.snapshot()["round"]}
            else:
                fail(
                    ErrorCode.INVALID_INPUT,
                    f"未知逻辑时钟命令: {command}",
                    scenario=scenario,
                    actual=command,
                )
            results.append(
                {
                    "at_ms": action.at_ms,
                    "command": command,
                    "result": result,
                }
            )
        return {
            "ok": True,
            "scenario": scenario,
            "actions": results,
            "final_status": self._read_round()["status"],
        }
