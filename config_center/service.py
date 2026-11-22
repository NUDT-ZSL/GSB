from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .compute import (
    compute_bundle,
    compare_metric_sets,
    conclusion_signature,
    make_probe_bundle,
    normalize_events,
)
from .errors import (
    already_initialized,
    bad_clock,
    bad_config,
    not_found,
    not_initialized,
    round_conflict,
    version_conflict,
    version_rollback,
)
from .fixed import convert_decl, stable_decimal
from .models import (
    ConfigDecl,
    build_patch_configs,
    normalize_patch,
    normalize_snapshot,
    snapshot_digest,
)
from .records import ConfigBundle
from .store import CenterStore, now_utc
from .util import (
    canonical_json,
    decimal_text,
    read_json,
    sha256_json,
    strict_loads_bytes,
    to_jsonable,
)


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ConfigCenter:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        data_dir: str | Path | None = None,
    ) -> None:
        env_root = os.environ.get("CONFIG_CENTER_ROOT")
        self.root = Path(root or env_root or Path.cwd()).resolve()
        self.data_dir = (
            Path(data_dir).resolve()
            if data_dir is not None
            else self.root / "data"
        )
        self.store = CenterStore(self.root)

    # ------------------------------------------------------------------
    # Input files
    # ------------------------------------------------------------------
    def load_file(self, path: str | Path) -> dict[str, Any]:
        return self.load_snapshot_document(read_json(path), source=str(path))

    def apply_file(self, path: str | Path) -> dict[str, Any]:
        return self.apply_patch_document(read_json(path), source=str(path))

    def compute_file(
        self,
        path: str | Path,
        *,
        client_id: str | None = None,
        round_key: str | None = None,
        namespace: str | None = None,
        delay_ms: int = 0,
        crash_at: str | None = None,
    ) -> dict[str, Any]:
        return self.compute_document(
            read_json(path),
            client_id=client_id,
            round_key=round_key,
            namespace=namespace,
            delay_ms=delay_ms,
            crash_at=crash_at,
        )

    # ------------------------------------------------------------------
    # Snapshot loading
    # ------------------------------------------------------------------
    def load_snapshot_document(
        self,
        raw: Any,
        *,
        source: str = "snapshot",
    ) -> dict[str, Any]:
        snapshot = normalize_snapshot(raw)
        with self.store.write_session() as conn:
            pointer = self.store.get_pointer(conn, snapshot.namespace)
            if pointer is not None:
                if snapshot.version == int(pointer["version"]):
                    if snapshot.digest == pointer["digest"]:
                        return {
                            "ok": True,
                            "status": "noop",
                            "idempotent": True,
                            "namespace": snapshot.namespace,
                            "version": snapshot.version,
                            "digest": snapshot.digest,
                            "change_kind": "noop",
                        }
                    raise version_conflict(
                        "同一版本号不能加载不同内容摘要",
                        namespace=snapshot.namespace,
                        version=snapshot.version,
                        current_digest=pointer["digest"],
                        requested_digest=snapshot.digest,
                    )
                if snapshot.version < int(pointer["version"]):
                    raise version_rollback(
                        "初始快照版本低于当前版本，回退被拒绝",
                        namespace=snapshot.namespace,
                        current_version=int(pointer["version"]),
                        requested_version=snapshot.version,
                    )
                raise already_initialized(
                    "命名空间已初始化，请使用 apply 提交后续版本",
                    namespace=snapshot.namespace,
                    current_version=int(pointer["version"]),
                    requested_version=snapshot.version,
                )

            rows = self._build_version_rows(
                conn,
                namespace=snapshot.namespace,
                version=snapshot.version,
                configs=snapshot.configs,
                previous=None,
                force_convert=True,
            )
            detail = {
                "added_keys": sorted(snapshot.configs),
                "removed_keys": [],
                "changed_keys": sorted(snapshot.configs),
                "same_keys": [],
                "digest_equal": False,
                "version_delta": snapshot.version,
            }
            self._insert_version(
                conn,
                namespace=snapshot.namespace,
                version=snapshot.version,
                parent_version=None,
                digest=snapshot.digest,
                logical_clock_ms=snapshot.logical_clock_ms,
                change_kind="initial",
                detail=detail,
                rows=rows,
                full_replace=True,
                source=source,
                patch_digest=None,
            )
            self.store.update_pointer(
                conn,
                namespace=snapshot.namespace,
                version=snapshot.version,
                digest=snapshot.digest,
                logical_clock_ms=snapshot.logical_clock_ms,
            )
        return {
            "ok": True,
            "status": "loaded",
            "idempotent": False,
            "namespace": snapshot.namespace,
            "version": snapshot.version,
            "digest": snapshot.digest,
            "logical_clock_ms": snapshot.logical_clock_ms,
            "change_kind": "initial",
            "config_count": len(snapshot.configs),
        }

    # ------------------------------------------------------------------
    # Patch application
    # ------------------------------------------------------------------
    def apply_patch_document(
        self,
        raw: Any,
        *,
        source: str = "patch",
    ) -> dict[str, Any]:
        patch = normalize_patch(raw)
        with self.store.write_session() as conn:
            pointer = self.store.get_pointer(conn, patch.namespace)
            if pointer is None:
                raise not_initialized(
                    f"命名空间 {patch.namespace} 尚未加载初始快照"
                )
            current_version = int(pointer["version"])
            current_clock = int(pointer["logical_clock_ms"])

            if patch.target_version < current_version:
                raise version_rollback(
                    "补丁目标版本低于当前版本，回退被拒绝",
                    namespace=patch.namespace,
                    current_version=current_version,
                    target_version=patch.target_version,
                )
            if patch.target_version == current_version:
                current_meta = self.store.get_version_meta(
                    conn, patch.namespace, current_version
                )
                if (
                    current_meta is not None
                    and current_meta["patch_digest"] == patch.patch_digest
                ):
                    return {
                        "ok": True,
                        "status": "noop",
                        "idempotent": True,
                        "namespace": patch.namespace,
                        "version": current_version,
                        "digest": pointer["digest"],
                        "change_kind": "noop",
                        "target_version": patch.target_version,
                    }
                raise version_conflict(
                    "同一版本号已提交不同补丁",
                    namespace=patch.namespace,
                    version=current_version,
                    current_patch_digest=(
                        current_meta["patch_digest"] if current_meta else None
                    ),
                    requested_patch_digest=patch.patch_digest,
                )
            if self.store.get_version_meta(
                conn, patch.namespace, patch.target_version
            ) is not None:
                raise version_conflict(
                    "目标版本号已经存在",
                    namespace=patch.namespace,
                    target_version=patch.target_version,
                )
            if patch.base_version is not None and patch.base_version != current_version:
                if patch.base_version < current_version:
                    raise version_rollback(
                        "补丁基线版本低于当前版本",
                        namespace=patch.namespace,
                        current_version=current_version,
                        base_version=patch.base_version,
                    )
                raise bad_config(
                    "补丁基线版本高于当前版本",
                    current_version=current_version,
                    base_version=patch.base_version,
                )
            if patch.logical_clock_ms < current_clock:
                raise bad_clock(
                    "补丁逻辑时钟低于当前水位",
                    current_clock=current_clock,
                    requested_clock=patch.logical_clock_ms,
                )

            current_bundle = self.store.read_bundle(
                conn, patch.namespace, current_version
            )
            if current_bundle is None:
                raise not_found(
                    "当前版本数据缺失",
                    namespace=patch.namespace,
                    version=current_version,
                )
            current_decls = {
                key: cfg.declaration() for key, cfg in current_bundle.configs.items()
            }
            new_decls = build_patch_configs(patch, current_decls)
            digest = snapshot_digest(
                patch.namespace,
                patch.target_version,
                patch.logical_clock_ms,
                new_decls,
            )
            old_keys = set(current_decls)
            new_keys = set(new_decls)
            added = sorted(new_keys - old_keys)
            removed = sorted(old_keys - new_keys)
            changed = sorted(
                key
                for key in old_keys & new_keys
                if current_decls[key].to_dict() != new_decls[key].to_dict()
            )
            same = sorted(
                key
                for key in old_keys & new_keys
                if current_decls[key].to_dict() == new_decls[key].to_dict()
            )
            if patch.mode == "replace":
                change_kind = "full_replace"
            elif added and not removed:
                change_kind = "incremental_append"
            else:
                change_kind = "full_replace"
            force_convert = change_kind == "full_replace"
            detail = {
                "added_keys": added,
                "removed_keys": removed,
                "changed_keys": changed,
                "same_keys": same,
                "digest_equal": digest == pointer["digest"],
                "version_delta": patch.target_version - current_version,
                "selection": (
                    "key_set_strict_superset_without_removals"
                    if change_kind == "incremental_append"
                    else "replace_or_same_key_set"
                ),
            }
            rows = self._build_version_rows(
                conn,
                namespace=patch.namespace,
                version=patch.target_version,
                configs=new_decls,
                previous=current_bundle,
                force_convert=force_convert,
            )
            self._insert_version(
                conn,
                namespace=patch.namespace,
                version=patch.target_version,
                parent_version=current_version,
                digest=digest,
                logical_clock_ms=patch.logical_clock_ms,
                change_kind=change_kind,
                detail=detail,
                rows=rows,
                full_replace=change_kind == "full_replace",
                source=source,
                patch_digest=patch.patch_digest,
            )
            self.store.update_pointer(
                conn,
                namespace=patch.namespace,
                version=patch.target_version,
                digest=digest,
                logical_clock_ms=patch.logical_clock_ms,
            )
        return {
            "ok": True,
            "status": "applied",
            "idempotent": False,
            "namespace": patch.namespace,
            "version": patch.target_version,
            "parent_version": current_version,
            "digest": digest,
            "patch_digest": patch.patch_digest,
            "logical_clock_ms": patch.logical_clock_ms,
            "change_kind": change_kind,
            "change_detail": detail,
            "config_count": len(new_decls),
        }

    def _build_version_rows(
        self,
        conn: Any,
        *,
        namespace: str,
        version: int,
        configs: dict[str, ConfigDecl],
        previous: ConfigBundle | None,
        force_convert: bool,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        previous_configs = previous.configs if previous is not None else {}
        for key, decl in sorted(configs.items()):
            prior = previous_configs.get(key)
            inherited = (
                prior is not None
                and not force_convert
                and prior.declaration() == decl
            )
            if inherited and prior is not None:
                rows.append(
                    {
                        "namespace": namespace,
                        "version": version,
                        "config_key": key,
                        "value_type": decl.value_type,
                        "value_text": decl.value_text,
                        "unit_scale": decl.scale,
                        "role": decl.role,
                        "fixed_int_text": (
                            str(prior.fixed_int)
                            if prior.fixed_int is not None
                            else None
                        ),
                        "residual_units_text": decimal_text(
                            prior.residual_units
                        ),
                        "residual_value_text": decimal_text(
                            prior.residual_value
                        ),
                        "conversion_id": prior.conversion_id,
                    }
                )
                continue

            if not decl.is_numeric:
                rows.append(
                    {
                        "namespace": namespace,
                        "version": version,
                        "config_key": key,
                        "value_type": decl.value_type,
                        "value_text": decl.value_text,
                        "unit_scale": decl.scale,
                        "role": decl.role,
                        "fixed_int_text": None,
                        "residual_units_text": "0",
                        "residual_value_text": "0",
                        "conversion_id": None,
                    }
                )
                continue
            carried_in = (
                prior.residual_units
                if prior is not None and prior.value_decimal is not None
                else Decimal(0)
            )
            record = convert_decl(decl, carried_in)
            conversion_id = self.store.insert_conversion(
                conn,
                namespace=namespace,
                version=version,
                config_key=key,
                value_type=decl.value_type,
                scale=decl.scale,
                value_text=decl.value_text,
                raw_scaled_text=stable_decimal(record.raw_scaled),
                carried_in_text=stable_decimal(record.carried_in),
                adjusted_scaled_text=stable_decimal(record.adjusted_scaled),
                fixed_int_text=str(record.fixed_int),
                discarded_text=stable_decimal(record.discarded),
                carried_out_text=stable_decimal(record.carried_out),
                rounding_direction=record.rounding_direction,
                conversion_error_text=stable_decimal(record.conversion_error),
                residual_value_text=stable_decimal(record.residual_value),
            )
            rows.append(
                {
                    "namespace": namespace,
                    "version": version,
                    "config_key": key,
                    "value_type": decl.value_type,
                    "value_text": decl.value_text,
                    "unit_scale": decl.scale,
                    "role": decl.role,
                    "fixed_int_text": str(record.fixed_int),
                    "residual_units_text": stable_decimal(
                        record.carried_out
                    ),
                    "residual_value_text": stable_decimal(
                        record.residual_value
                    ),
                    "conversion_id": conversion_id,
                }
            )
        return rows

    def _insert_version(
        self,
        conn: Any,
        *,
        namespace: str,
        version: int,
        parent_version: int | None,
        digest: str,
        logical_clock_ms: int,
        change_kind: str,
        detail: dict[str, Any],
        rows: list[dict[str, Any]],
        full_replace: bool,
        source: str,
        patch_digest: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO versions (
                namespace, version, parent_version, digest, content_digest,
                patch_digest, logical_clock_ms, change_kind,
                change_detail_json, config_count, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                namespace,
                version,
                parent_version,
                digest,
                digest,
                patch_digest,
                logical_clock_ms,
                change_kind,
                canonical_json(detail),
                len(rows),
                source,
                now_utc(),
            ),
        )
        self.store.insert_version_configs(conn, rows, full_replace=full_replace)

    # ------------------------------------------------------------------
    # Compute / crash recovery
    # ------------------------------------------------------------------
    def compute_document(
        self,
        raw: Any,
        *,
        client_id: str | None = None,
        round_key: str | None = None,
        namespace: str | None = None,
        delay_ms: int = 0,
        crash_at: str | None = None,
    ) -> dict[str, Any]:
        if crash_at not in {None, "after-start", "before-commit", "after-commit"}:
            raise bad_config(
                "crash_at 仅支持 after-start / before-commit / after-commit",
                crash_at=crash_at,
            )
        if delay_ms < 0:
            raise bad_config("delay_ms 不能为负", delay_ms=delay_ms)
        spec = self._normalize_compute_spec(
            raw,
            client_id=client_id,
            round_key=round_key,
            namespace=namespace,
        )
        return self._execute_compute(
            spec,
            delay_ms=delay_ms,
            crash_at=crash_at,
            resume_job=None,
        )

    def _normalize_compute_spec(
        self,
        raw: Any,
        *,
        client_id: str | None,
        round_key: str | None,
        namespace: str | None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise bad_config("compute 输入必须是 JSON 对象")
        resolved_namespace = namespace or raw.get("namespace")
        if not isinstance(resolved_namespace, str) or not _ID_RE.fullmatch(
            resolved_namespace
        ):
            raise bad_config(
                "compute 输入缺少合法 namespace",
                namespace=resolved_namespace,
            )
        resolved_client = client_id or raw.get("client_id")
        if not isinstance(resolved_client, str) or not _ID_RE.fullmatch(resolved_client):
            raise bad_config("client_id 必须匹配 [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
        resolved_round = round_key or raw.get("round_key")
        if not isinstance(resolved_round, str) or not _ID_RE.fullmatch(resolved_round):
            raise bad_config("round_key 必须匹配 [A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")

        events = normalize_events(raw.get("events"))
        tolerance_raw = raw.get("tolerance", "0.001")
        if isinstance(tolerance_raw, bool):
            raise bad_config("tolerance 必须是非负十进制数")
        try:
            tolerance = (
                tolerance_raw
                if isinstance(tolerance_raw, Decimal)
                else Decimal(str(tolerance_raw))
            )
        except InvalidOperation as exc:
            raise bad_config("tolerance 必须是非负十进制数", tolerance=tolerance_raw) from exc
        if not tolerance.is_finite() or tolerance < 0:
            raise bad_config("tolerance 必须是非负十进制数", tolerance=tolerance_raw)

        clock_raw = raw.get("logical_clock_ms")
        explicit_clock: int | None = None
        if clock_raw is not None:
            if isinstance(clock_raw, bool) or not isinstance(clock_raw, int):
                raise bad_clock("logical_clock_ms 必须是整数")
            if clock_raw < 0:
                raise bad_clock("logical_clock_ms 不能为负")
            explicit_clock = clock_raw

        return {
            "namespace": resolved_namespace,
            "client_id": resolved_client,
            "round_key": resolved_round,
            "events": events,
            "tolerance": tolerance,
            "explicit_clock": explicit_clock,
        }

    def _materialize_input(
        self,
        spec: dict[str, Any],
        *,
        logical_clock_ms: int,
    ) -> dict[str, Any]:
        event_payload = [
            {
                "id": event.event_id,
                "value": event.value_text,
                "weight": event.weight_text,
            }
            for event in spec["events"]
        ]
        semantic = {
            "namespace": spec["namespace"],
            "client_id": spec["client_id"],
            "round_key": spec["round_key"],
            "events": event_payload,
            "tolerance": stable_decimal(spec["tolerance"]),
        }
        payload = {
            **semantic,
            "logical_clock_ms": logical_clock_ms,
        }
        return {
            **payload,
            "input_digest": sha256_json(semantic),
            "input_json": canonical_json(payload),
        }

    def _clock_for_round(
        self,
        round_key: str,
        spec: dict[str, Any],
        pointer_clock: int,
    ) -> int:
        if spec["explicit_clock"] is not None:
            return int(spec["explicit_clock"])
        steps_path = self.data_dir / "clock_steps.json"
        if steps_path.exists():
            steps = read_json(steps_path)
            if isinstance(steps, dict) and "steps" in steps:
                steps = steps["steps"]
            if isinstance(steps, dict):
                value = steps.get(round_key)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise bad_clock(
                            "clock_steps.json 中的逻辑时钟必须是非负整数",
                            round_key=round_key,
                            value=value,
                        )
                    return value
        return pointer_clock

    def _execute_compute(
        self,
        spec: dict[str, Any],
        *,
        delay_ms: int,
        crash_at: str | None,
        resume_job: dict[str, Any] | None,
    ) -> dict[str, Any]:
        namespace = spec["namespace"]
        client_id = spec["client_id"]
        round_key = spec["round_key"]

        with self.store.write_session() as conn:
            existing_result = self.store.get_result(
                conn, namespace, client_id, round_key
            )
            job = resume_job or self.store.get_job(
                conn, namespace, client_id, round_key
            )
            if existing_result is not None:
                if job is None:
                    raise round_conflict(
                        "结果存在但计算作业缺失",
                        client_id=client_id,
                        round_key=round_key,
                    )
                requested_digest = self._semantic_digest_from_spec(spec)
                if job["input_digest"] != requested_digest:
                    raise round_conflict(
                        "同一客户端轮次已提交不同输入",
                        client_id=client_id,
                        round_key=round_key,
                        existing_input_digest=job["input_digest"],
                        requested_input_digest=requested_digest,
                    )
                if job["input_digest"] != existing_result["input_digest"]:
                    raise round_conflict(
                        "同一客户端轮次的结果摘要冲突",
                        client_id=client_id,
                        round_key=round_key,
                    )
                return {
                    "ok": True,
                    "status": "committed",
                    "idempotent": True,
                    "bound_version": existing_result["bound_version"],
                    "result": json.loads(existing_result["result_json"]),
                }

            if job is None:
                pointer = self.store.get_pointer(conn, namespace)
                if pointer is None:
                    raise not_initialized(
                        f"命名空间 {namespace} 尚未加载初始配置"
                    )
                pointer_clock = int(pointer["logical_clock_ms"])
                clock = self._clock_for_round(round_key, spec, pointer_clock)
                materialized = self._materialize_input(
                    spec, logical_clock_ms=clock
                )
                bound_version = int(pointer["version"])
                bound_digest = pointer["digest"]
                now = now_utc()
                conn.execute(
                    """
                    INSERT INTO compute_jobs (
                        namespace, client_id, round_key, bound_version,
                        bound_digest, input_digest, input_json, logical_clock_ms,
                        status, attempt_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                    """,
                    (
                        namespace,
                        client_id,
                        round_key,
                        bound_version,
                        bound_digest,
                        materialized["input_digest"],
                        materialized["input_json"],
                        clock,
                        now,
                        now,
                    ),
                )
                job = self.store.get_job(conn, namespace, client_id, round_key)
            else:
                if job["input_digest"] != self._semantic_digest_from_spec(spec):
                    raise round_conflict(
                        "同一客户端轮次已绑定不同输入",
                        client_id=client_id,
                        round_key=round_key,
                        existing_input_digest=job["input_digest"],
                        requested_input_digest=self._semantic_digest_from_spec(spec),
                    )
                if job["status"] == "committed":
                    existing = self.store.get_result(
                        conn, namespace, client_id, round_key
                    )
                    if existing is None:
                        raise round_conflict(
                            "作业已提交但结果缺失",
                            client_id=client_id,
                            round_key=round_key,
                        )
                    return {
                        "ok": True,
                        "status": "committed",
                        "idempotent": True,
                        "bound_version": existing["bound_version"],
                        "result": json.loads(existing["result_json"]),
                    }
                conn.execute(
                    """
                    UPDATE compute_jobs
                    SET attempt_count = attempt_count + 1, updated_at = ?
                    WHERE namespace = ? AND client_id = ? AND round_key = ?
                    """,
                    (now_utc(), namespace, client_id, round_key),
                )
                job = self.store.get_job(conn, namespace, client_id, round_key)

            assert job is not None
            bound_version = int(job["bound_version"])
            input_payload = json.loads(job["input_json"])
            bound_digest = job["bound_digest"]
            clock = int(job["logical_clock_ms"])
            input_digest = job["input_digest"]

        bundle = self.store.read_version_bundle(namespace, bound_version)
        if bundle is None:
            raise not_found(
                "绑定版本不存在",
                namespace=namespace,
                version=bound_version,
            )

        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        if crash_at == "after-start":
            os._exit(86)

        result = compute_bundle(
            bundle,
            normalize_events(input_payload["events"]),
            tolerance=Decimal(str(input_payload["tolerance"])),
            logical_clock_ms=clock,
            input_digest=input_digest,
        )
        if crash_at == "before-commit":
            os._exit(86)

        with self.store.write_session() as conn:
            existing = self.store.get_result(conn, namespace, client_id, round_key)
            if existing is not None:
                return {
                    "ok": True,
                    "status": "committed",
                    "idempotent": True,
                    "bound_version": existing["bound_version"],
                    "result": json.loads(existing["result_json"]),
                }
            job = self.store.get_job(conn, namespace, client_id, round_key)
            if job is None:
                raise round_conflict(
                    "提交时计算作业缺失",
                    client_id=client_id,
                    round_key=round_key,
                )
            if job["status"] == "committed":
                existing = self.store.get_result(
                    conn, namespace, client_id, round_key
                )
                if existing is not None:
                    return {
                        "ok": True,
                        "status": "committed",
                        "idempotent": True,
                        "bound_version": existing["bound_version"],
                        "result": json.loads(existing["result_json"]),
                    }
                raise round_conflict("作业状态与结果不一致")
            result_json = canonical_json(result)
            cursor = conn.execute(
                """
                INSERT INTO client_results (
                    namespace, client_id, round_key, bound_version,
                    bound_digest, input_digest, result_digest, result_json,
                    error_ledger_json, precision_sensitive, logical_clock_ms,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    namespace,
                    client_id,
                    round_key,
                    bound_version,
                    bound_digest,
                    input_digest,
                    result["result_digest"],
                    result_json,
                    canonical_json(result["error_ledger"]),
                    1 if result["sensitivity"]["precision_sensitive"] else 0,
                    clock,
                    now_utc(),
                ),
            )
            result_id = int(cursor.lastrowid)
            for row in result["error_ledger"]:
                conn.execute(
                    """
                    INSERT INTO error_ledger (
                        result_id, metric, config_key, source_version,
                        conversion_id, scale, residual_units_text,
                        residual_value_text, contribution_text, formula
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_id,
                        row["metric"],
                        row["config_key"],
                        row.get("version"),
                        row.get("conversion_id"),
                        row.get("scale"),
                        row.get("residual_units"),
                        row.get("residual"),
                        row.get("contribution"),
                        row["formula"],
                    ),
                )
            now = now_utc()
            conn.execute(
                """
                UPDATE compute_jobs
                SET status = 'committed', updated_at = ?, committed_at = ?
                WHERE namespace = ? AND client_id = ? AND round_key = ?
                """,
                (now, now, namespace, client_id, round_key),
            )
            conn.execute(
                """
                INSERT INTO clock_watermarks (
                    namespace, client_id, logical_clock_ms, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, client_id) DO UPDATE SET
                    logical_clock_ms = MAX(
                        clock_watermarks.logical_clock_ms,
                        excluded.logical_clock_ms
                    ),
                    updated_at = excluded.updated_at
                """,
                (namespace, client_id, clock, now),
            )

        if crash_at == "after-commit":
            os._exit(0)
        return {
            "ok": True,
            "status": "committed",
            "idempotent": False,
            "namespace": namespace,
            "client_id": client_id,
            "round_key": round_key,
            "bound_version": bound_version,
            "bound_digest": bound_digest,
            "logical_clock_ms": clock,
            "result": result,
        }

    def _semantic_digest_from_spec(self, spec: dict[str, Any]) -> str:
        semantic = {
            "namespace": spec["namespace"],
            "client_id": spec["client_id"],
            "round_key": spec["round_key"],
            "events": [
                {
                    "id": event.event_id,
                    "value": event.value_text,
                    "weight": event.weight_text,
                }
                for event in spec["events"]
            ],
            "tolerance": stable_decimal(spec["tolerance"]),
        }
        return sha256_json(semantic)

    def resume(
        self,
        *,
        namespace: str | None = None,
        client_id: str | None = None,
        round_key: str | None = None,
    ) -> dict[str, Any]:
        pending = self.store.list_pending_jobs(
            namespace,
            client_id=client_id,
            round_key=round_key,
        )
        resumed: list[dict[str, Any]] = []
        for job in pending:
            payload = json.loads(job["input_json"])
            spec = {
                "namespace": job["namespace"],
                "client_id": job["client_id"],
                "round_key": job["round_key"],
                "events": normalize_events(payload["events"]),
                "tolerance": Decimal(str(payload["tolerance"])),
                "explicit_clock": int(job["logical_clock_ms"]),
            }
            response = self._execute_compute(
                spec,
                delay_ms=0,
                crash_at=None,
                resume_job=job,
            )
            resumed.append(
                {
                    "namespace": job["namespace"],
                    "client_id": job["client_id"],
                    "round_key": job["round_key"],
                    "bound_version": job["bound_version"],
                    "status": response["status"],
                    "idempotent": response["idempotent"],
                }
            )
        return {
            "ok": True,
            "resumed_count": len(resumed),
            "resumed": resumed,
            "pending_after": len(
                self.store.list_pending_jobs(
                    namespace,
                    client_id=client_id,
                    round_key=round_key,
                )
            ),
        }

    # ------------------------------------------------------------------
    # Status / diagnostics
    # ------------------------------------------------------------------
    def status(self, namespace: str | None = None) -> dict[str, Any]:
        if namespace is None:
            pointers = self.store.list_pointers()
            if len(pointers) == 1:
                namespace = pointers[0]["namespace"]
        data = self.store.status(namespace)
        pointers = data["pointers"]
        pointer = pointers[0] if len(pointers) == 1 else None
        max_client_clock = max(
            (
                int(item["logical_clock_ms"])
                for item in data["client_watermarks"]
            ),
            default=0,
        )
        config_clock = int(pointer["logical_clock_ms"]) if pointer else None
        watermark = (
            max(config_clock, max_client_clock)
            if config_clock is not None
            else max_client_clock
        )
        data.update(
            {
                "ok": True,
                "namespace": namespace,
                "current_version": (
                    int(pointer["version"]) if pointer is not None else None
                ),
                "current_digest": (
                    pointer["digest"] if pointer is not None else None
                ),
                "config_version_clock_ms": config_clock,
                "logical_clock_watermark": watermark,
                "version_count": len(data["versions"]),
            }
        )
        return data

    def compare_versions(
        self,
        namespace: str,
        from_version: int,
        to_version: int,
        raw_input: Any,
        *,
        tolerance: Decimal | None = None,
        scale_factor: int = 100,
    ) -> dict[str, Any]:
        if scale_factor <= 0:
            raise bad_config("scale_factor 必须大于 0")
        spec = self._normalize_compute_spec(
            raw_input,
            client_id="version-comparator",
            round_key=f"compare-{from_version}-{to_version}",
            namespace=namespace,
        )
        if tolerance is None:
            tolerance = spec["tolerance"]
        left_bundle = self.store.read_version_bundle(namespace, from_version)
        right_bundle = self.store.read_version_bundle(namespace, to_version)
        if left_bundle is None:
            raise not_found(
                "比较源版本不存在", namespace=namespace, version=from_version
            )
        if right_bundle is None:
            raise not_found(
                "比较目标版本不存在", namespace=namespace, version=to_version
            )
        input_digest = sha256_json(
            {
                "namespace": namespace,
                "events": [
                    {
                        "id": event.event_id,
                        "value": event.value_text,
                        "weight": event.weight_text,
                    }
                    for event in spec["events"]
                ],
                "tolerance": stable_decimal(tolerance),
            }
        )
        clock = max(left_bundle.logical_clock_ms, right_bundle.logical_clock_ms)
        left = compute_bundle(
            left_bundle,
            spec["events"],
            tolerance=tolerance,
            logical_clock_ms=clock,
            input_digest=input_digest,
        )
        right = compute_bundle(
            right_bundle,
            spec["events"],
            tolerance=tolerance,
            logical_clock_ms=clock,
            input_digest=input_digest,
        )
        comparison = compare_metric_sets(
            left,
            right,
            tolerance=tolerance,
            left_label=f"v{from_version}",
            right_label=f"v{to_version}",
        )
        high = self._higher_scale_comparison(
            left_bundle,
            right_bundle,
            spec["events"],
            tolerance=tolerance,
            input_digest=input_digest,
            scale_factor=scale_factor,
            comparison=comparison,
        )
        return {
            "ok": True,
            "namespace": namespace,
            "from_version": from_version,
            "to_version": to_version,
            "tolerance": stable_decimal(tolerance),
            "comparison": comparison,
            "higher_scale": high,
            "left": {
                "bound_version": left["bound_version"],
                "result_digest": left["result_digest"],
                "sensitivity": left["sensitivity"],
                "metrics": left["metrics"],
            },
            "right": {
                "bound_version": right["bound_version"],
                "result_digest": right["result_digest"],
                "sensitivity": right["sensitivity"],
                "metrics": right["metrics"],
            },
        }

    def compare_clients(
        self,
        namespace: str,
        left_client: str,
        left_round: str,
        right_client: str,
        right_round: str,
        *,
        tolerance: Decimal | None = None,
        scale_factor: int = 100,
    ) -> dict[str, Any]:
        left_job = self.store.get_job_input(namespace, left_client, left_round)
        right_job = self.store.get_job_input(namespace, right_client, right_round)
        if left_job is None:
            raise not_found(
                "左侧客户端轮次不存在",
                client_id=left_client,
                round_key=left_round,
            )
        if right_job is None:
            raise not_found(
                "右侧客户端轮次不存在",
                client_id=right_client,
                round_key=right_round,
            )
        if tolerance is None:
            tolerance = min(
                Decimal(str(left_job["input"]["tolerance"])),
                Decimal(str(right_job["input"]["tolerance"])),
            )
        left = self._recompute_job(left_job, tolerance)
        right = self._recompute_job(right_job, tolerance)
        comparison = compare_metric_sets(
            left["result"],
            right["result"],
            tolerance=tolerance,
            left_label=f"{left_client}/{left_round}",
            right_label=f"{right_client}/{right_round}",
        )
        high = self._higher_scale_comparison(
            left["bundle"],
            right["bundle"],
            left["events"],
            tolerance=tolerance,
            input_digest=sha256_json(
                {
                    "left": left["input_digest"],
                    "right": right["input_digest"],
                }
            ),
            scale_factor=scale_factor,
            comparison=comparison,
            right_events=right["events"],
        )
        return {
            "ok": True,
            "namespace": namespace,
            "left": {
                "client_id": left_client,
                "round_key": left_round,
                "bound_version": left["result"]["bound_version"],
                "result_digest": left["result"]["result_digest"],
                "metrics": left["result"]["metrics"],
            },
            "right": {
                "client_id": right_client,
                "round_key": right_round,
                "bound_version": right["result"]["bound_version"],
                "result_digest": right["result"]["result_digest"],
                "metrics": right["result"]["metrics"],
            },
            "tolerance": stable_decimal(tolerance),
            "comparison": comparison,
            "higher_scale": high,
        }

    def _recompute_job(
        self,
        job: dict[str, Any],
        tolerance: Decimal,
    ) -> dict[str, Any]:
        bundle = self.store.read_version_bundle(
            job["namespace"], int(job["bound_version"])
        )
        if bundle is None:
            raise not_found(
                "客户端绑定版本不存在",
                namespace=job["namespace"],
                version=job["bound_version"],
            )
        events = normalize_events(job["input"]["events"])
        result = compute_bundle(
            bundle,
            events,
            tolerance=tolerance,
            logical_clock_ms=int(job["logical_clock_ms"]),
            input_digest=job["input_digest"],
        )
        return {
            "bundle": bundle,
            "events": events,
            "input_digest": job["input_digest"],
            "result": result,
        }

    def _higher_scale_comparison(
        self,
        left_bundle: ConfigBundle,
        right_bundle: ConfigBundle,
        left_events: list[Any],
        *,
        tolerance: Decimal,
        input_digest: str,
        scale_factor: int,
        comparison: dict[str, Any],
        right_events: list[Any] | None = None,
    ) -> dict[str, Any]:
        right_events = right_events or left_events
        clock = max(left_bundle.logical_clock_ms, right_bundle.logical_clock_ms)
        left_probe = compute_bundle(
            make_probe_bundle(left_bundle, scale_factor),
            left_events,
            tolerance=tolerance,
            logical_clock_ms=clock,
            input_digest=input_digest,
            input_scale_factor=scale_factor,
            probe=True,
        )
        right_probe = compute_bundle(
            make_probe_bundle(right_bundle, scale_factor),
            right_events,
            tolerance=tolerance,
            logical_clock_ms=clock,
            input_digest=input_digest,
            input_scale_factor=scale_factor,
            probe=True,
        )
        left_high_sig = conclusion_signature(
            {
                **left_probe,
                "exact_conclusion": left_probe["fixed_conclusion"],
            }
        )
        right_high_sig = conclusion_signature(
            {
                **right_probe,
                "exact_conclusion": right_probe["fixed_conclusion"],
            }
        )
        baseline_equal = (
            comparison["metrics"]
            and all(
                row["fixed_delta"] == "0"
                for row in comparison["metrics"]
                if row["metric"] in {"threshold", "expected_batch", "output_count"}
            )
        )
        high_equal = left_high_sig == right_high_sig
        flips = bool(comparison["precision_sensitive"] and not baseline_equal and high_equal)
        if flips:
            explanation = (
                f"将各配置标度与输入标度放大 {scale_factor} 倍后，两侧离散结论变为一致"
                f"（过滤 {left_high_sig['filtered_ids']}，处理 "
                f"{left_high_sig['processed_ids']}，输出 {left_high_sig['output_count']}），"
                "说明低成本标度下的结论会被更高标度翻转。"
            )
        elif comparison["precision_sensitive"]:
            explanation = (
                f"标度放大 {scale_factor} 倍后两侧结论仍不一致，当前精度敏感不能仅靠提高标度消除。"
            )
        else:
            explanation = (
                f"基线未观察到精度敏感，标度放大 {scale_factor} 倍后结论保持一致。"
            )
        return {
            "scale_factor": scale_factor,
            "left_input_scale": left_probe["input_scale"],
            "right_input_scale": right_probe["input_scale"],
            "left_fixed_conclusion": left_high_sig,
            "right_fixed_conclusion": right_high_sig,
            "fixed_conclusions_equal": high_equal,
            "flips_conclusion": flips,
            "explanation": explanation,
        }

    # ------------------------------------------------------------------
    # Demo helpers
    # ------------------------------------------------------------------
    def patch_digest(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise bad_config("补丁必须是 JSON 对象")
        body = dict(raw)
        body.pop("content_digest", None)
        digest = sha256_json(body)
        body["content_digest"] = digest
        return {
            "ok": True,
            "content_digest": digest,
            "patch": body,
            "patch_text": json.dumps(
                to_jsonable(body),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
        }

    def reset(self) -> dict[str, Any]:
        self.store.reset_business_state()
        return {"ok": True, "status": "reset"}

    def bootstrap_demo(self, *, include_patches: bool = True) -> dict[str, Any]:
        self.store.reset_business_state()
        snapshot_path = self.data_dir / "snapshot.json"
        if not snapshot_path.exists():
            raise not_found("默认样本快照不存在", path=str(snapshot_path))
        loaded = self.load_file(snapshot_path)
        results: list[dict[str, Any]] = []
        if include_patches:
            for patch_path in sorted((self.data_dir / "patches").glob("*.json")):
                results.append(self.apply_file(patch_path))
        return {
            "ok": True,
            "status": "bootstrapped",
            "loaded": loaded,
            "patches": results,
        }


def parse_json_bytes(data: bytes) -> Any:
    try:
        return strict_loads_bytes(data)
    except ValueError as exc:
        raise bad_config("请求体不是合法 JSON", reason=str(exc)) from exc

