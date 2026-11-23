from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import BusinessError, ErrorCode
from .invalidation import compare_columns, compute_invalidation
from .locking import FileLock
from .models import PlanRecord, ViewDef
from .planner import PlanBuilder, build_plans
from .snapshots import SnapshotBundle, load_snapshot_bundle
from .storage import (
    Storage,
    _fault,
    now_ms,
    state_digest,
    view_set_digest,
)
from .util import (
    canonical_json,
    digest_object,
    normalize_digest,
    read_json_file,
)
from .validation import (
    validate_dependencies,
    validate_sql_source_declarations,
    validate_view_collection,
    validate_view_dict,
)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return _is_int(value) and value >= 0


def _copy_error(exc: BusinessError, **extra: Any) -> BusinessError:
    details = dict(exc.details)
    details.update({key: value for key, value in extra.items() if value is not None})
    return BusinessError(exc.code, exc.message, details)


class AppService:
    def __init__(
        self,
        *,
        db_path: str | Path = "runtime/state.db",
        lock_path: str | Path = "runtime/app.lock",
        log_dir: str | Path = "runtime/logs",
        lock_timeout_seconds: float = 15.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)
        self.log_dir = Path(log_dir)
        self.lock_timeout_seconds = lock_timeout_seconds
        self.storage = Storage(self.db_path, timeout_seconds=lock_timeout_seconds)

    def _locked(self) -> FileLock:
        return FileLock(self.lock_path, timeout_seconds=self.lock_timeout_seconds)

    def _log(self, command: str, payload: Mapping[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "at_ms": now_ms(),
            "pid": os.getpid(),
            "command": command,
            "payload": payload,
        }
        path = self.log_dir / "sqlplan.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
            handle.flush()

    def _recover_locked(self) -> Dict[str, Any]:
        return self.storage.resume_pending()

    def _read_state_or_none(self) -> Optional[Dict[str, Any]]:
        if not self.storage.exists():
            return None
        self.storage.require_initialized()
        state = self.storage.read_state()
        if state["plan_generation"] == 0 and not state["records"]:
            if state["pending_runs"] or state["pending_invalidation_events"]:
                return state
            return None
        return state

    def _load_validate(
        self,
        *,
        views_path: str | Path,
        v1_path: str | Path,
        v2_dir: str | Path,
        v2_meta_path: Optional[str | Path],
        injected_clock_ms: Optional[int],
    ) -> Tuple[Dict[str, ViewDef], Dict[str, PlanRecord], int, SnapshotBundle]:
        views, watermark = validate_view_collection(read_json_file(views_path))
        records = PlanBuilder(views).build_all()
        bundle = load_snapshot_bundle(
            views=views,
            records=records,
            v1_path=v1_path,
            v2_dir=v2_dir,
            v2_meta_path=v2_meta_path,
            view_watermark=watermark,
            injected_clock_ms=injected_clock_ms,
        )
        tombstoned = {
            name
            for name, entry in bundle.merged.get("views", {}).items()
            if name in views and entry.get("tombstone")
        }
        if tombstoned:
            views = {
                name: view for name, view in views.items() if name not in tombstoned
            }
            validate_dependencies(views)
            validate_sql_source_declarations(views)
            records = PlanBuilder(views).build_all()
        stale_snapshot_views = sorted(
            name
            for name, entry in bundle.merged.get("views", {}).items()
            if name in views
            and entry.get("snapshot_cache_verdict") == "recompute_stale"
        )
        bundle.merged["snapshot_recompute_required"] = stale_snapshot_views
        return views, records, watermark, bundle

    def load(
        self,
        *,
        views_path: str | Path,
        v1_path: str | Path,
        v2_dir: str | Path,
        v2_meta_path: Optional[str | Path] = None,
        injected_clock_ms: Optional[int] = None,
        force_decision: Optional[str] = None,
    ) -> Dict[str, Any]:
        if injected_clock_ms is not None and not _is_nonnegative_int(injected_clock_ms):
            raise BusinessError(
                ErrorCode.E_BAD_CLOCK,
                "注入逻辑时钟必须是非负整数",
                {"value": injected_clock_ms},
            )
        # Validation and deterministic plan construction happen before any DB write.
        views, incoming_records, _view_watermark, bundle = self._load_validate(
            views_path=views_path,
            v1_path=v1_path,
            v2_dir=v2_dir,
            v2_meta_path=v2_meta_path,
            injected_clock_ms=injected_clock_ms,
        )
        incoming_view_set_digest = view_set_digest(views)
        incoming_state_digest = state_digest(incoming_records)
        incoming_content_digest = bundle.content_digest
        package_patch_id = "load:" + incoming_content_digest

        with self._locked():
            if self.storage.exists():
                self.storage.require_initialized()
                self._recover_locked()
            else:
                self.storage.initialize()
            state = self._read_state_or_none()
            current_generation = state["plan_generation"] if state is not None else 0
            incoming_generation = max(bundle.source_plan_generation, 1)
            existing_run = self.storage.find_run(package_patch_id)
            patch_digest = "sha256:" + digest_object(
                {
                    "content_digest": incoming_content_digest,
                    "view_set_digest": incoming_view_set_digest,
                    "source_plan_generation": incoming_generation,
                }
            )

            if state is not None and incoming_generation < current_generation:
                raise BusinessError(
                    ErrorCode.E_GENERATION_ROLLBACK,
                    "拒绝版本回退的快照包",
                    {
                        "current_plan_generation": current_generation,
                        "incoming_plan_generation": incoming_generation,
                        "db": str(self.db_path),
                    },
                )
            if state is not None and incoming_generation == current_generation:
                last_package_digest = self.storage.latest_package_content_digest(
                    current_generation
                )
                package_matches = last_package_digest is None or normalize_digest(
                    last_package_digest
                ) == normalize_digest(incoming_content_digest)
                if (
                    state["view_set_digest"] == incoming_view_set_digest
                    and state["snapshot_digest"] == incoming_state_digest
                    and package_matches
                ):
                    result = {
                        "ok": True,
                        "duplicate": False,
                        "decision": "noop",
                        "plan_generation": current_generation,
                        "logical_clock_watermark": state["logical_clock_watermark"],
                        "view_count": len(state["views"]),
                        "snapshot_merge": bundle.merged,
                    }
                    self._log("load", {"result": result})
                    return result
                raise BusinessError(
                    ErrorCode.E_GENERATION_CONFLICT,
                    "相同计划代数对应不同快照内容",
                    {
                        "plan_generation": current_generation,
                        "current_view_set_digest": state["view_set_digest"],
                        "incoming_view_set_digest": incoming_view_set_digest,
                        "current_state_digest": state["snapshot_digest"],
                        "incoming_state_digest": incoming_state_digest,
                        "current_package_content_digest": last_package_digest,
                        "incoming_package_content_digest": incoming_content_digest,
                    },
                )
            if (
                state is not None
                and existing_run is not None
                and existing_run["status"] == "committed"
                and existing_run["target_generation"] == current_generation
            ):
                result = json.loads(existing_run["result_json"])
                result["duplicate"] = True
                return result

            if state is None:
                decision = "initial_load"
                target_generation = incoming_generation
            elif set(state["records"]).issubset(incoming_records) and all(
                state["views"][name].content_digest == views[name].content_digest
                and state["records"][name].plan_digest
                == incoming_records[name].plan_digest
                for name in state["records"]
            ):
                decision = "incremental_append"
                target_generation = incoming_generation
            else:
                decision = "complete_replace"
                target_generation = incoming_generation
            if force_decision is not None:
                if force_decision not in {
                    "incremental_append",
                    "complete_replace",
                }:
                    raise BusinessError(
                        ErrorCode.E_BAD_SNAPSHOT,
                        "force_decision 非法",
                        {"value": force_decision},
                    )
                decision = force_decision
            if target_generation <= current_generation:
                raise BusinessError(
                    ErrorCode.E_GENERATION_ROLLBACK,
                    "快照计划代数未前进",
                    {
                        "current_plan_generation": current_generation,
                        "incoming_plan_generation": target_generation,
                    },
                )

            if state is None:
                invalidation = {
                    "patch_id": package_patch_id,
                    "edit_round": target_generation,
                    "triggers": [],
                    "column_changes": {},
                    "recomputed_views": sorted(incoming_records),
                    "reused_views": [],
                    "deleted_views": [],
                    "recomputed_executable_sql": sorted(incoming_records),
                    "reused_executable_sql": [],
                    "events": [],
                    "propagation_paths": {},
                    "summary": {
                        "event_count": 0,
                        "plan_invalidated_count": 0,
                        "column_resolution_invalidated_count": 0,
                        "executable_sql_invalidated_count": 0,
                        "reused_plan_count": 0,
                        "recomputed_plan_count": len(incoming_records),
                    },
                }
            else:
                changed_triggers = {
                    name: {
                        "type": "snapshot_import",
                        "view": name,
                        "patch_id": package_patch_id,
                        "edit_round": target_generation,
                    }
                    for name in sorted(set(state["views"]) | set(views))
                    if name not in state["views"]
                    or name not in views
                    or state["views"][name].content_digest != views[name].content_digest
                    or (
                        name in state["records"]
                        and name in incoming_records
                        and state["records"][name].plan_digest
                        != incoming_records[name].plan_digest
                    )
                }
                invalidation = compute_invalidation(
                    old_views=state["views"],
                    new_views=views,
                    old_records=state["records"],
                    new_records=incoming_records,
                    triggers=changed_triggers,
                    patch_id=package_patch_id,
                    edit_round=target_generation,
                )

            result_events = []
            for event in invalidation["events"]:
                event_copy = dict(event)
                event_copy["status"] = "applied"
                result_events.append(event_copy)
            invalidation_for_result = dict(invalidation)
            invalidation_for_result["events"] = result_events
            watermark = max(
                bundle.logical_clock_watermark,
                state["logical_clock_watermark"] if state is not None else 0,
            )
            result = {
                "decision": decision,
                "duplicate": False,
                "base_plan_generation": current_generation,
                "plan_generation": target_generation,
                "logical_clock_watermark": watermark,
                "view_count": len(incoming_records),
                "view_set_digest": incoming_view_set_digest,
                "snapshot_content_digest": incoming_content_digest,
                "snapshot_recompute_required": bundle.merged.get(
                    "snapshot_recompute_required", []
                ),
                "invalidation": invalidation_for_result,
                "invalidations": invalidation_for_result,
                "snapshot_merge": bundle.merged,
            }
            payload = {
                "views": [views[name].to_dict() for name in sorted(views)],
                "records": [
                    incoming_records[name].to_dict()
                    for name in sorted(incoming_records)
                ],
                "events": invalidation["events"],
                "state_digest": incoming_state_digest,
                "snapshot_content_digest": incoming_content_digest,
                "result": result,
                "snapshot_package": {
                    "package_digest": incoming_content_digest,
                    "content_digest": incoming_content_digest,
                    "view_set_digest": incoming_view_set_digest,
                    "decision": decision,
                    "payload": bundle.to_dict(),
                },
            }
            run_id = "load-" + incoming_content_digest.split(":")[-1][:24]
            self.storage.prepare_run(
                run_id=run_id,
                kind="load",
                patch_id=package_patch_id,
                patch_digest=patch_digest,
                edit_round=target_generation,
                base_generation=current_generation,
                target_generation=target_generation,
                logical_clock_ms=watermark,
                payload=payload,
            )
            _fault("after_prepare")
            self.storage.stage_run(run_id)
            self.storage.finalize_run(run_id)
            final = json.loads(self.storage.find_run_by_id(run_id)["result_json"])
            self._log("load", {"result": final})
            return final

    def _parse_patch(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise BusinessError(ErrorCode.E_BAD_PATCH, "补丁必须是 JSON 对象")
        patch_id = raw.get("patch_id")
        if not isinstance(patch_id, str) or not patch_id:
            raise BusinessError(
                ErrorCode.E_BAD_PATCH,
                "补丁缺少非空 patch_id",
            )
        operations = raw.get("operations")
        if not isinstance(operations, list) or not operations:
            raise BusinessError(
                ErrorCode.E_BAD_PATCH,
                "operation 必须是非空数组",
                {"patch_id": patch_id},
            )
        edit_round = raw.get("edit_round")
        if edit_round is not None and not _is_nonnegative_int(edit_round):
            raise BusinessError(
                ErrorCode.E_BAD_PATCH,
                "edit_round 必须是非负整数",
                {"patch_id": patch_id, "value": edit_round},
            )
        patch_clock = raw.get("logical_clock_ms")
        if patch_clock is not None and not _is_nonnegative_int(patch_clock):
            raise BusinessError(
                ErrorCode.E_BAD_CLOCK,
                "补丁 logical_clock_ms 必须是非负整数",
                {"patch_id": patch_id, "value": patch_clock},
            )
        for field in ("base_plan_generation", "plan_generation"):
            value = raw.get(field)
            if value is not None and not _is_nonnegative_int(value):
                raise BusinessError(
                    ErrorCode.E_BAD_PATCH,
                    f"{field} 必须是非负整数",
                    {"patch_id": patch_id, "field": field, "value": value},
                )
        return {
            "raw": dict(raw),
            "patch_id": patch_id,
            "operations": operations,
            "edit_round": edit_round,
            "logical_clock_ms": patch_clock,
            "base_plan_generation": raw.get("base_plan_generation"),
            "plan_generation": raw.get("plan_generation"),
        }

    def _operation_view(
        self,
        operation: Mapping[str, Any],
        current: Optional[ViewDef],
        patch_clock: Optional[int],
        *,
        op_index: int,
    ) -> ViewDef:
        nested = operation.get("view")
        source: Dict[str, Any]
        if nested is not None:
            if not isinstance(nested, Mapping):
                raise BusinessError(
                    ErrorCode.E_BAD_VIEW,
                    "operation.view 必须是对象",
                    {"operation_index": op_index},
                )
            source = dict(nested)
        else:
            source = {
                key: value
                for key, value in operation.items()
                if key not in {"op", "name", "view", "cascade"}
            }
        if "name" in operation:
            if "name" in source and source["name"] != operation["name"]:
                raise BusinessError(
                    ErrorCode.E_BAD_VIEW,
                    "operation.name 与 view.name 不一致",
                    {
                        "operation_index": op_index,
                        "operation_name": operation["name"],
                        "view_name": source["name"],
                    },
                )
            source["name"] = operation["name"]
        if "content_digest" not in source:
            clock = source.get("logical_clock_ms", patch_clock)
            name = source.get("name")
            sql = source.get("sql")
            upstream = source.get("upstream", [])
            if (
                isinstance(name, str)
                and isinstance(sql, str)
                and isinstance(upstream, list)
                and _is_nonnegative_int(clock)
            ):
                source["content_digest"] = "sha256:" + digest_object(
                    {
                        "name": name,
                        "sql": sql,
                        "upstream": list(upstream),
                        "logical_clock_ms": clock,
                    }
                )
        if current is not None:
            merged = current.to_dict()
            merged.update(source)
            if patch_clock is not None and "logical_clock_ms" not in source:
                merged["logical_clock_ms"] = patch_clock
            if "content_digest" not in source:
                merged.pop("content_digest", None)
                if (
                    isinstance(merged.get("name"), str)
                    and isinstance(merged.get("sql"), str)
                    and isinstance(merged.get("upstream"), (list, tuple))
                    and _is_nonnegative_int(merged.get("logical_clock_ms"))
                ):
                    merged["content_digest"] = "sha256:" + digest_object(
                        {
                            "name": merged["name"],
                            "sql": merged["sql"],
                            "upstream": list(merged["upstream"]),
                            "logical_clock_ms": merged["logical_clock_ms"],
                        }
                    )
            parsed = validate_view_dict(
                merged,
                inherited_clock=patch_clock,
                verify_digest=True,
            )
            return ViewDef(**parsed)
        parsed = validate_view_dict(
            source,
            inherited_clock=patch_clock,
            verify_digest=True,
        )
        return ViewDef(**parsed)

    def _augment_operation_error(
        self,
        exc: BusinessError,
        *,
        patch_id: str,
        edit_round: int,
        op_index: int,
        operation: Mapping[str, Any],
        trigger_view: Optional[str],
        trial_views: Optional[Mapping[str, ViewDef]],
        old_record: Optional[PlanRecord],
        trial_records: Optional[Mapping[str, PlanRecord]],
        available_records: Optional[Mapping[str, PlanRecord]],
    ) -> BusinessError:
        details = dict(exc.details)
        details.update(
            {
                "patch_id": patch_id,
                "edit_round": edit_round,
                "operation_index": op_index,
                "operation": operation.get("op"),
                "trigger_view": trigger_view,
            }
        )
        if (
            trigger_view
            and old_record is not None
            and trial_views is not None
            and trigger_view in trial_views
        ):
            trial_record = (
                trial_records[trigger_view]
                if trial_records is not None and trigger_view in trial_records
                else None
            )
            if trial_record is None:
                try:
                    trial_record = PlanBuilder(
                        trial_views,
                        upstream_plans=available_records or {},
                    ).build_view(
                        trial_views[trigger_view],
                        upstream_plans=available_records or {},
                    )
                except BusinessError:
                    trial_record = None
            if trial_record is not None:
                changes = compare_columns(old_record.columns, trial_record.columns)
                details["removed_columns"] = changes["removed"]
                details["renamed_columns"] = changes["renamed"]
                details["changed_columns"] = changes["changed"]
        if exc.code == ErrorCode.E_COLUMN_NOT_FOUND:
            details.setdefault("downstream_view", exc.details.get("view"))
            details.setdefault(
                "reference_position", exc.details.get("reference_position")
            )
        return BusinessError(exc.code, exc.message, details)

    def edit(self, patch: str | Path | Mapping[str, Any]) -> Dict[str, Any]:
        if isinstance(patch, (str, Path)):
            raw = read_json_file(patch)
        else:
            raw = patch
        parsed_patch = self._parse_patch(raw)
        patch_id = parsed_patch["patch_id"]
        patch_digest = "sha256:" + digest_object(parsed_patch["raw"])
        self.storage.require_initialized()

        with self._locked():
            self._recover_locked()
            state = self.storage.read_state()
            existing = self.storage.find_run(patch_id)
            if existing is not None:
                if normalize_digest(existing["patch_digest"]) != normalize_digest(
                    patch_digest
                ):
                    raise BusinessError(
                        ErrorCode.E_PATCH_CONFLICT,
                        "patch_id 已用于不同内容的补丁",
                        {
                            "patch_id": patch_id,
                            "existing_digest": existing["patch_digest"],
                            "incoming_digest": patch_digest,
                        },
                    )
                result = json.loads(existing["result_json"])
                result["duplicate"] = True
                self._log("edit", {"result": result})
                return result

            current_generation = state["plan_generation"]
            edit_round = (
                parsed_patch["edit_round"]
                if parsed_patch["edit_round"] is not None
                else current_generation + 1
            )
            base_generation = parsed_patch["base_plan_generation"]
            if base_generation is not None and base_generation != current_generation:
                raise BusinessError(
                    ErrorCode.E_BASE_GENERATION_MISMATCH,
                    "补丁基线与当前计划代数不一致",
                    {
                        "patch_id": patch_id,
                        "edit_round": edit_round,
                        "expected_base_generation": base_generation,
                        "current_generation": current_generation,
                    },
                )
            requested_generation = parsed_patch["plan_generation"]
            if (
                requested_generation is not None
                and requested_generation <= current_generation
            ):
                raise BusinessError(
                    ErrorCode.E_GENERATION_ROLLBACK,
                    "补丁请求的计划代数发生回退",
                    {
                        "patch_id": patch_id,
                        "edit_round": edit_round,
                        "current_generation": current_generation,
                        "requested_generation": requested_generation,
                    },
                )
            target_generation = (
                requested_generation
                if requested_generation is not None
                else current_generation + 1
            )
            patch_clock = parsed_patch["logical_clock_ms"]
            if (
                patch_clock is not None
                and patch_clock < state["logical_clock_watermark"]
            ):
                raise BusinessError(
                    ErrorCode.E_CLOCK_REGRESSION,
                    "补丁逻辑时钟低于当前水位",
                    {
                        "patch_id": patch_id,
                        "edit_round": edit_round,
                        "patch_clock_ms": patch_clock,
                        "watermark_ms": state["logical_clock_watermark"],
                    },
                )

            candidate_views: Dict[str, ViewDef] = dict(state["views"])
            candidate_records: Dict[str, PlanRecord] = dict(state["records"])
            operation_results: List[Dict[str, Any]] = []
            accepted_triggers: Dict[str, Dict[str, Any]] = {}
            rejected_errors: List[BusinessError] = []

            def merge_trigger(
                target: Dict[str, Dict[str, Any]],
                view_name: str,
                trigger_type: str,
                operation_index: int,
            ) -> None:
                if view_name in target:
                    previous = target[view_name]
                    previous["type"] = trigger_type
                    indices = previous.setdefault(
                        "operation_indices",
                        [previous["operation_index"]],
                    )
                    if operation_index not in indices:
                        indices.append(operation_index)
                else:
                    target[view_name] = {
                        "type": trigger_type,
                        "view": view_name,
                        "patch_id": patch_id,
                        "edit_round": edit_round,
                        "operation_index": operation_index,
                    }

            batch_views: Dict[str, ViewDef] = dict(state["views"])
            batch_triggers: Dict[str, Dict[str, Any]] = {}
            batch_operations: List[Dict[str, Any]] = []
            batch_structurally_valid = True
            for index, raw_operation in enumerate(parsed_patch["operations"]):
                operation = (
                    dict(raw_operation) if isinstance(raw_operation, Mapping) else {}
                )
                op_kind = operation.get("op")
                op_name = operation.get("name")
                if isinstance(operation.get("view"), Mapping):
                    op_name = op_name or operation["view"].get("name")
                if (
                    not isinstance(op_kind, str)
                    or op_kind not in {"add", "modify", "delete"}
                    or not isinstance(op_name, str)
                ):
                    batch_structurally_valid = False
                    break
                try:
                    if op_kind == "delete":
                        if op_name not in batch_views:
                            raise BusinessError(
                                ErrorCode.E_VIEW_NOT_FOUND,
                                "待删除视图不存在",
                            )
                        batch_views.pop(op_name, None)
                        trigger_view = op_name
                        trigger_type = "view_deleted"
                    elif op_kind == "add":
                        if op_name in batch_views:
                            raise BusinessError(
                                ErrorCode.E_VIEW_EXISTS,
                                "新增视图已存在",
                            )
                        new_view = self._operation_view(
                            operation, None, patch_clock, op_index=index
                        )
                        batch_views[new_view.name] = new_view
                        trigger_view = new_view.name
                        trigger_type = "view_added"
                    else:
                        if op_name not in batch_views:
                            raise BusinessError(
                                ErrorCode.E_VIEW_NOT_FOUND,
                                "待修改视图不存在",
                            )
                        new_view = self._operation_view(
                            operation,
                            batch_views[op_name],
                            patch_clock,
                            op_index=index,
                        )
                        if new_view.name != op_name:
                            raise BusinessError(
                                ErrorCode.E_BAD_VIEW,
                                "modify 不允许隐式改名；请使用删除加新增",
                            )
                        batch_views[new_view.name] = new_view
                        trigger_view = new_view.name
                        trigger_type = "view_modified"
                except BusinessError:
                    batch_structurally_valid = False
                    break
                merge_trigger(batch_triggers, trigger_view, trigger_type, index)
                batch_operations.append(
                    {
                        "index": index,
                        "op": op_kind,
                        "name": trigger_view,
                    }
                )

            batch_applied = False
            if batch_structurally_valid:
                try:
                    validate_dependencies(batch_views)
                    validate_sql_source_declarations(batch_views)
                    batch_records = build_plans(batch_views, previous=state["records"])
                    for trigger_view in batch_triggers:
                        if (
                            trigger_view in batch_records
                            and batch_records[trigger_view].logical_clock_ms
                            < state["logical_clock_watermark"]
                        ):
                            raise BusinessError(
                                ErrorCode.E_CLOCK_REGRESSION,
                                "视图逻辑时钟低于当前水位",
                            )
                except BusinessError:
                    pass
                else:
                    candidate_views = batch_views
                    candidate_records = batch_records
                    accepted_triggers = batch_triggers
                    operation_results = [
                        {
                            **item,
                            "status": "accepted",
                            "plan_digest": (
                                candidate_records[item["name"]].plan_digest
                                if item["name"] in candidate_records
                                else None
                            ),
                        }
                        for item in batch_operations
                    ]
                    batch_applied = True

            for index, operation in (
                [] if batch_applied else enumerate(parsed_patch["operations"])
            ):
                operation = dict(operation) if isinstance(operation, Mapping) else {}
                op_kind = operation.get("op")
                op_name = operation.get("name")
                if isinstance(operation.get("view"), Mapping):
                    op_name = op_name or operation["view"].get("name")
                if not isinstance(op_kind, str) or op_kind not in {
                    "add",
                    "modify",
                    "delete",
                }:
                    error = BusinessError(
                        ErrorCode.E_BAD_PATCH,
                        "op 必须是 add、modify 或 delete",
                        {
                            "patch_id": patch_id,
                            "edit_round": edit_round,
                            "operation_index": index,
                            "op": op_kind,
                        },
                    )
                    rejected_errors.append(error)
                    operation_results.append(
                        {
                            "index": index,
                            "op": op_kind,
                            "name": op_name,
                            "status": "rejected",
                            "error": error.to_dict()["error"],
                        }
                    )
                    continue
                trial_views: Optional[Dict[str, ViewDef]] = None
                trial_records: Optional[Dict[str, PlanRecord]] = None
                trigger_view = op_name if isinstance(op_name, str) else None
                old_record = (
                    candidate_records.get(op_name) if isinstance(op_name, str) else None
                )
                try:
                    if op_kind == "delete":
                        if (
                            not isinstance(op_name, str)
                            or op_name not in candidate_views
                        ):
                            raise BusinessError(
                                ErrorCode.E_VIEW_NOT_FOUND,
                                "待删除视图不存在",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                    "view": op_name,
                                },
                            )
                        dependents = sorted(
                            view.name
                            for view in candidate_views.values()
                            if op_name in view.upstream
                        )
                        if dependents:
                            raise BusinessError(
                                ErrorCode.E_VIEW_IN_USE,
                                "视图仍被下游视图依赖，不能删除",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                    "view": op_name,
                                    "downstream_views": dependents,
                                },
                            )
                        trial_views = dict(candidate_views)
                        trial_views.pop(op_name, None)
                        trigger_type = "view_deleted"
                        old_record = candidate_records.get(op_name)
                        trigger_view = op_name
                    elif op_kind == "add":
                        if not isinstance(op_name, str) or not op_name:
                            raise BusinessError(
                                ErrorCode.E_BAD_VIEW,
                                "add 操作缺少视图名",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                },
                            )
                        if op_name in candidate_views:
                            raise BusinessError(
                                ErrorCode.E_VIEW_EXISTS,
                                "新增视图已存在",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                    "view": op_name,
                                },
                            )
                        new_view = self._operation_view(
                            operation,
                            None,
                            patch_clock,
                            op_index=index,
                        )
                        trial_views = dict(candidate_views)
                        trial_views[new_view.name] = new_view
                        trigger_type = "view_added"
                        old_record = None
                        trigger_view = new_view.name
                    else:
                        if (
                            not isinstance(op_name, str)
                            or op_name not in candidate_views
                        ):
                            raise BusinessError(
                                ErrorCode.E_VIEW_NOT_FOUND,
                                "待修改视图不存在",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                    "view": op_name,
                                },
                            )
                        new_view = self._operation_view(
                            operation,
                            candidate_views[op_name],
                            patch_clock,
                            op_index=index,
                        )
                        if new_view.name != op_name:
                            raise BusinessError(
                                ErrorCode.E_BAD_VIEW,
                                "modify 不允许隐式改名；请使用删除加新增",
                                {
                                    "patch_id": patch_id,
                                    "edit_round": edit_round,
                                    "operation_index": index,
                                    "old_name": op_name,
                                    "new_name": new_view.name,
                                },
                            )
                        trial_views = dict(candidate_views)
                        trial_views[new_view.name] = new_view
                        trigger_type = "view_modified"
                        old_record = candidate_records.get(op_name)
                        trigger_view = new_view.name

                    if (
                        patch_clock is not None
                        and patch_clock < state["logical_clock_watermark"]
                    ):
                        raise BusinessError(
                            ErrorCode.E_CLOCK_REGRESSION,
                            "操作逻辑时钟低于当前水位",
                            {
                                "patch_id": patch_id,
                                "edit_round": edit_round,
                                "operation_index": index,
                            },
                        )
                    validate_dependencies(trial_views)
                    validate_sql_source_declarations(trial_views)
                    trial_records = build_plans(trial_views, previous=candidate_records)
                    if (
                        trigger_view in trial_records
                        and trial_records[trigger_view].logical_clock_ms
                        < state["logical_clock_watermark"]
                    ):
                        raise BusinessError(
                            ErrorCode.E_CLOCK_REGRESSION,
                            "视图逻辑时钟低于当前水位",
                            {
                                "patch_id": patch_id,
                                "edit_round": edit_round,
                                "operation_index": index,
                                "view": trigger_view,
                                "view_clock_ms": trial_records[
                                    trigger_view
                                ].logical_clock_ms,
                                "watermark_ms": state["logical_clock_watermark"],
                            },
                        )
                except BusinessError as exc:
                    error = self._augment_operation_error(
                        exc,
                        patch_id=patch_id,
                        edit_round=edit_round,
                        op_index=index,
                        operation=operation,
                        trigger_view=trigger_view,
                        trial_views=trial_views,
                        old_record=old_record,
                        trial_records=trial_records,
                        available_records=candidate_records,
                    )
                    rejected_errors.append(error)
                    operation_results.append(
                        {
                            "index": index,
                            "op": op_kind,
                            "name": op_name,
                            "status": "rejected",
                            "error": error.to_dict()["error"],
                        }
                    )
                    continue

                candidate_views = trial_views
                candidate_records = trial_records
                merge_trigger(
                    accepted_triggers,
                    trigger_view,
                    trigger_type,
                    index,
                )
                operation_results.append(
                    {
                        "index": index,
                        "op": op_kind,
                        "name": trigger_view,
                        "status": "accepted",
                        "plan_digest": (
                            candidate_records[trigger_view].plan_digest
                            if trigger_view in candidate_records
                            else None
                        ),
                    }
                )

            if not accepted_triggers:
                if rejected_errors:
                    first = rejected_errors[0]
                    first.details["operation_errors"] = [
                        error.to_dict()["error"] for error in rejected_errors
                    ]
                    if len(parsed_patch["operations"]) > 1:
                        first.details["operations"] = operation_results
                    raise first
                raise BusinessError(
                    ErrorCode.E_BAD_PATCH,
                    "补丁没有可执行操作",
                    {"patch_id": patch_id},
                )

            invalidation = compute_invalidation(
                old_views=state["views"],
                new_views=candidate_views,
                old_records=state["records"],
                new_records=candidate_records,
                triggers=accepted_triggers,
                patch_id=patch_id,
                edit_round=edit_round,
            )
            result_events = []
            for event in invalidation["events"]:
                event_copy = dict(event)
                event_copy["status"] = "applied"
                result_events.append(event_copy)
            invalidation_result = dict(invalidation)
            invalidation_result["events"] = result_events
            watermark = max(
                [
                    state["logical_clock_watermark"],
                    patch_clock or 0,
                ]
                + [view.logical_clock_ms for view in candidate_views.values()]
            )
            new_state_digest = state_digest(candidate_records)
            result = {
                "edit_round": edit_round,
                "base_plan_generation": current_generation,
                "plan_generation": target_generation,
                "logical_clock_watermark": watermark,
                "operations": operation_results,
                "accepted_operations": [
                    item for item in operation_results if item["status"] == "accepted"
                ],
                "rejected_operations": [
                    item for item in operation_results if item["status"] == "rejected"
                ],
                "invalidations": invalidation_result,
                "invalidation": invalidation_result,
                "recomputed_views": invalidation["recomputed_views"],
                "reused_views": invalidation["reused_views"],
                "deleted_views": invalidation["deleted_views"],
                "view_set_digest": view_set_digest(candidate_views),
            }
            payload = {
                "views": [
                    candidate_views[name].to_dict() for name in sorted(candidate_views)
                ],
                "records": [
                    candidate_records[name].to_dict()
                    for name in sorted(candidate_records)
                ],
                "events": invalidation["events"],
                "state_digest": new_state_digest,
                "snapshot_content_digest": state["snapshot_digest"],
                "result": result,
            }
            run_id = (
                "edit-"
                + (
                    "sha256:"
                    + digest_object(
                        {"patch_id": patch_id, "target_generation": target_generation}
                    )
                ).split(":")[-1][:32]
            )
            self.storage.prepare_run(
                run_id=run_id,
                kind="edit",
                patch_id=patch_id,
                patch_digest=patch_digest,
                edit_round=edit_round,
                base_generation=current_generation,
                target_generation=target_generation,
                logical_clock_ms=watermark,
                payload=payload,
            )
            _fault("after_prepare")
            self.storage.stage_run(run_id)
            self.storage.finalize_run(run_id)
            final = json.loads(self.storage.find_run_by_id(run_id)["result_json"])
            self._log("edit", {"result": final})
            return final

    def status(self) -> Dict[str, Any]:
        with self._locked():
            state = self._read_state_or_none()
            if state is None:
                raise BusinessError(
                    ErrorCode.E_NOT_LOADED,
                    "尚未加载视图与快照",
                    {"db": str(self.db_path)},
                )
            views = []
            for name in sorted(state["records"]):
                record = state["records"][name]
                views.append(
                    {
                        "view": name,
                        "name": name,
                        "plan_digest": record.plan_digest,
                        "logical_clock_ms": record.logical_clock_ms,
                        "upstream": list(record.upstream),
                        "sql_digest": record.sql_digest,
                        "content_digest": record.content_digest,
                    }
                )
            result = {
                "ok": True,
                "plan_generation": state["plan_generation"],
                "current_plan_generation": state["plan_generation"],
                "logical_clock_watermark": state["logical_clock_watermark"],
                "pending_invalidation_events": state["pending_invalidation_events"],
                "pending_events": state["pending_invalidation_events"],
                "pending_runs": state["pending_runs"],
                "view_count": len(views),
                "views": views,
                "committed_generation_count": state["committed_generation_count"],
                "db": str(self.db_path),
            }
        self._log("status", {"plan_generation": result["current_plan_generation"]})
        return result

    def plan(self, view_name: Optional[str] = None) -> Dict[str, Any]:
        with self._locked():
            state = self._read_state_or_none()
            if state is None:
                raise BusinessError(
                    ErrorCode.E_NOT_LOADED,
                    "尚未加载视图与快照",
                    {"db": str(self.db_path)},
                )
            if view_name is not None:
                if view_name not in state["records"]:
                    raise BusinessError(
                        ErrorCode.E_VIEW_NOT_FOUND,
                        "视图不存在",
                        {
                            "view": view_name,
                            "available_views": sorted(state["records"]),
                        },
                    )
                record = state["records"][view_name]
                return {
                    "ok": True,
                    "plan_generation": state["plan_generation"],
                    "view": view_name,
                    "plan_digest": record.plan_digest,
                    "columns": record.columns,
                    "plan": record.plan,
                    "executable_sql": record.executable_sql,
                }
            return {
                "ok": True,
                "plan_generation": state["plan_generation"],
                "views": [
                    {
                        "view": name,
                        "name": name,
                        "plan_digest": state["records"][name].plan_digest,
                        "columns": state["records"][name].columns,
                        "plan": state["records"][name].plan,
                        "executable_sql": state["records"][name].executable_sql,
                    }
                    for name in sorted(state["records"])
                ],
            }

    def resume(self) -> Dict[str, Any]:
        self.storage.require_initialized()
        with self._locked():
            recovered = self._recover_locked()
            state = self.storage.read_state()
            result = {
                "ok": True,
                "recovered_count": recovered["resumed_count"],
                "recovered": recovered["resumed"],
                "current_plan_generation": state["plan_generation"],
                "pending_invalidation_events": state["pending_invalidation_events"],
            }
            self._log("resume", result)
            return result

    def verify(
        self,
        *,
        sample_views_path: Optional[str | Path] = None,
        sample_v1_path: Optional[str | Path] = None,
        sample_v2_dir: Optional[str | Path] = None,
        sample_v2_meta_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        self.storage.require_initialized()
        with self._locked():
            return self._verify_locked(
                sample_views_path=sample_views_path,
                sample_v1_path=sample_v1_path,
                sample_v2_dir=sample_v2_dir,
                sample_v2_meta_path=sample_v2_meta_path,
            )

    def _verify_locked(
        self,
        *,
        sample_views_path: Optional[str | Path] = None,
        sample_v1_path: Optional[str | Path] = None,
        sample_v2_dir: Optional[str | Path] = None,
        sample_v2_meta_path: Optional[str | Path] = None,
    ) -> Dict[str, Any]:
        self.storage.require_initialized()
        checks: List[Dict[str, Any]] = []
        state = self._read_state_or_none()
        if state is None:
            raise BusinessError(
                ErrorCode.E_NOT_LOADED,
                "尚未加载视图与快照",
                {"db": str(self.db_path)},
            )
        with self.storage.connection() as connection:
            integrity_rows = [
                row[0] for row in connection.execute("PRAGMA integrity_check")
            ]
            generation_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT plan_generation, status FROM plan_generations "
                    "ORDER BY plan_generation"
                )
            ]
            plan_count = connection.execute(
                "SELECT COUNT(*) AS n FROM plans WHERE plan_generation = ?",
                (state["plan_generation"],),
            ).fetchone()["n"]
        checks.append(
            {
                "name": "sqlite_integrity",
                "ok": integrity_rows == ["ok"],
                "details": {"result": integrity_rows},
            }
        )
        checks.append(
            {
                "name": "pointer_plan_set_complete",
                "ok": plan_count == len(state["records"]),
                "details": {
                    "pointer_generation": state["plan_generation"],
                    "expected_views": len(state["records"]),
                    "plan_rows": plan_count,
                },
            }
        )
        generation_values = [row["plan_generation"] for row in generation_rows]
        checks.append(
            {
                "name": "generation_monotonic",
                "ok": generation_values == sorted(set(generation_values)),
                "details": {"generations": generation_values},
            }
        )
        checks.append(
            {
                "name": "generations_committed",
                "ok": all(row["status"] == "committed" for row in generation_rows),
                "details": {
                    "generations": [
                        {
                            "plan_generation": row["plan_generation"],
                            "status": row["status"],
                        }
                        for row in generation_rows
                    ]
                },
            }
        )
        try:
            recomputed = PlanBuilder(state["views"]).build_all()
        except BusinessError as exc:
            checks.append(
                {
                    "name": "plans_deterministic",
                    "ok": False,
                    "details": {"error": exc.to_dict()["error"]},
                }
            )
        else:
            plan_mismatches: List[Dict[str, Any]] = []
            for name in sorted(state["records"]):
                record = state["records"][name]
                fresh = recomputed.get(name)
                if fresh is None:
                    plan_mismatches.append({"view": name, "reason": "missing"})
                    continue
                reasons = []
                if canonical_json(record.plan) != canonical_json(fresh.plan):
                    reasons.append("canonical_plan")
                if record.plan_digest != fresh.plan_digest:
                    reasons.append("plan_digest")
                if record.executable_sql != fresh.executable_sql:
                    reasons.append("executable_sql")
                if canonical_json(record.columns) != canonical_json(fresh.columns):
                    reasons.append("columns")
                if reasons:
                    plan_mismatches.append({"view": name, "reasons": reasons})
            checks.append(
                {
                    "name": "plans_deterministic",
                    "ok": not plan_mismatches,
                    "details": {"mismatches": plan_mismatches},
                }
            )
        content_mismatches = []
        for name, view in sorted(state["views"].items()):
            calculated = view.calculated_content_digest()
            if normalize_digest(calculated) != normalize_digest(view.content_digest):
                content_mismatches.append(
                    {
                        "view": name,
                        "declared": view.content_digest,
                        "calculated": calculated,
                    }
                )
        checks.append(
            {
                "name": "view_content_digests",
                "ok": not content_mismatches,
                "details": {"mismatches": content_mismatches},
            }
        )
        pending_runs = state["pending_runs"]
        checks.append(
            {
                "name": "no_pending_runs",
                "ok": not pending_runs,
                "details": {"pending_runs": pending_runs},
            }
        )
        checks.append(
            {
                "name": "no_pending_events",
                "ok": state["pending_invalidation_events"] == 0,
                "details": {
                    "pending_invalidation_events": state["pending_invalidation_events"]
                },
            }
        )
        if sample_views_path is not None:
            try:
                (
                    sample_views,
                    sample_watermark,
                ) = validate_view_collection(read_json_file(sample_views_path))
                sample_records = PlanBuilder(sample_views).build_all()
                if sample_v1_path is not None and sample_v2_dir is not None:
                    load_snapshot_bundle(
                        views=sample_views,
                        records=sample_records,
                        v1_path=sample_v1_path,
                        v2_dir=sample_v2_dir,
                        v2_meta_path=sample_v2_meta_path,
                        view_watermark=sample_watermark,
                    )
                checks.append(
                    {
                        "name": "sample_snapshot_validation",
                        "ok": True,
                        "details": {
                            "views": len(sample_views),
                            "shards": "validated",
                        },
                    }
                )
            except BusinessError as exc:
                checks.append(
                    {
                        "name": "sample_snapshot_validation",
                        "ok": False,
                        "details": exc.to_dict()["error"],
                    }
                )
        ok = all(check["ok"] for check in checks)
        result = {
            "ok": ok,
            "plan_generation": state["plan_generation"],
            "checks": checks,
        }
        self._log("verify", {"ok": ok, "generation": state["plan_generation"]})
        return result
