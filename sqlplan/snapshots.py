from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import BusinessError, ErrorCode
from .models import PlanRecord, ViewDef
from .util import (
    digest_object,
    is_sha256,
    normalize_digest,
    read_json_file,
    sha256_bytes,
)


V1_FORMAT = "sqlplan/snapshot/v1"
V2_SHARD_FORMAT = "sqlplan/snapshot/v2-shard"


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_nonnegative_int(
    value: Any, field: str, *, source: str, view: Optional[str] = None
) -> int:
    if not _is_nonnegative_int(value):
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            f"{field} 必须是非负整数",
            {"source": source, "view": view, "field": field, "value": value},
        )
    return value


def _validate_declared_plan(
    view_name: str,
    declared: Any,
    records: Mapping[str, PlanRecord],
    *,
    source: str,
) -> Optional[bool]:
    if view_name not in records:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "快照引用了未声明视图",
            {"source": source, "view": view_name},
        )
    if declared is None:
        return None
    if not isinstance(declared, str) or not is_sha256(declared):
        raise BusinessError(
            ErrorCode.E_PLAN_DIGEST_MISMATCH,
            "快照计划摘要格式非法",
            {"source": source, "view": view_name, "plan_digest": declared},
        )
    calculated = records[view_name].plan_digest
    return normalize_digest(declared) == normalize_digest(calculated)


def _validate_column_descriptors(
    descriptors: Any,
    record: PlanRecord,
    *,
    source: str,
    flat_map: bool = False,
) -> None:
    if descriptors is None:
        return
    if flat_map:
        if not isinstance(descriptors, Mapping):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v1 column_map 必须是扁平对象",
                {"source": source},
            )
        items = []
        for key, descriptor in descriptors.items():
            if not isinstance(key, str) or key.count(".") != 1:
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v1 column_map 键必须为 view.column",
                    {"source": source, "key": key},
                )
            view_name, column_name = key.split(".", 1)
            if view_name != record.view_name:
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v1 column_map 与计划视图不一致",
                    {
                        "source": source,
                        "key": key,
                        "expected_view": record.view_name,
                    },
                )
            items.append((key, column_name, descriptor))
    else:
        if not isinstance(descriptors, list):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2 列映射必须是数组",
                {"source": source, "view": record.view_name},
            )
        items = []
        for index, descriptor in enumerate(descriptors):
            if not isinstance(descriptor, Mapping) or not isinstance(
                descriptor.get("name"), str
            ):
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v2 列映射条目缺少 name",
                    {"source": source, "view": record.view_name, "index": index},
                )
            items.append((descriptor["name"], descriptor["name"], descriptor))

    outputs = {column["name"]: column for column in record.columns}
    seen = set()
    for key, column_name, descriptor in items:
        if column_name not in outputs:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "快照列映射无法追溯到计划输出列",
                {
                    "source": source,
                    "view": record.view_name,
                    "column": column_name,
                    "key": key,
                    "available_columns": sorted(outputs),
                },
            )
        if column_name in seen:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "快照列映射包含重复列",
                {"source": source, "view": record.view_name, "column": column_name},
            )
        seen.add(column_name)
        if descriptor is None:
            continue
        if not isinstance(descriptor, Mapping):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "快照列描述必须是对象",
                {"source": source, "view": record.view_name, "column": column_name},
            )
        declared_view = descriptor.get("view")
        declared_column = descriptor.get("column")
        if declared_view is not None and declared_view != record.view_name:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "快照列描述中的视图不一致",
                {"source": source, "view": record.view_name, "column": column_name},
            )
        if declared_column is not None and declared_column != column_name:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "快照列描述中的列名不一致",
                {"source": source, "view": record.view_name, "column": column_name},
            )
        declared_digest = descriptor.get("expression_digest")
        if declared_digest is not None and not is_sha256(str(declared_digest)):
            raise BusinessError(
                ErrorCode.E_PLAN_DIGEST_MISMATCH,
                "快照列表达式摘要格式非法",
                {
                    "source": source,
                    "view": record.view_name,
                    "column": column_name,
                    "expression_digest": declared_digest,
                },
            )


@dataclass
class SnapshotCandidate:
    view_name: str
    source_version: str
    source_ref: str
    logical_clock_ms: int
    plan_generation: int
    plan_digest: Optional[str]
    columns: List[Dict[str, Any]]
    executable_sql: Optional[str]
    tombstone: bool
    original_time: Dict[str, Any]
    time_conversion: Optional[Dict[str, Any]] = None
    sequence: int = 0
    reason: Optional[str] = None
    digest_matches_current: Optional[bool] = None

    def sort_key(self) -> Tuple[int, int, int, int, str]:
        priority = 2 if self.source_version == "v2" else 1
        return (
            self.logical_clock_ms,
            self.plan_generation,
            priority,
            self.sequence,
            self.source_ref,
        )

    def audit(
        self, *, winner: bool = False, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        payload = {
            "view": self.view_name,
            "source_version": self.source_version,
            "source_ref": self.source_ref,
            "logical_clock_ms": self.logical_clock_ms,
            "plan_generation": self.plan_generation,
            "plan_digest": self.plan_digest,
            "tombstone": self.tombstone,
            "original_time": self.original_time,
            "time_conversion": self.time_conversion,
            "executable_sql_present": self.executable_sql is not None,
            "column_count": len(self.columns),
            "reason": self.reason,
            "digest_matches_current": self.digest_matches_current,
            "requires_recompute": self.digest_matches_current is False,
        }
        if winner:
            payload["selected"] = True
        elif reason:
            payload["selected"] = False
            payload["rejected_reason"] = reason
        return payload


@dataclass
class SnapshotBundle:
    v1: Dict[str, Any]
    v2: Dict[str, Any]
    merged: Dict[str, Any]
    source_plan_generation: int
    logical_clock_watermark: int
    content_digest: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "v1": self.v1,
            "v2": self.v2,
            "merged": self.merged,
            "source_plan_generation": self.source_plan_generation,
            "logical_clock_watermark": self.logical_clock_watermark,
            "content_digest": self.content_digest,
        }


def _convert_relative_ms(
    relative: Any, base_clock_ms: int
) -> Tuple[int, Dict[str, Any]]:
    if isinstance(relative, bool) or not isinstance(relative, (int, float)):
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "generated_at_relative_seconds 必须是数值",
            {"value": relative},
        )
    decimal_value = Decimal(str(relative))
    if not decimal_value.is_finite() or decimal_value < 0:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "相对生成秒数必须是有限的非负数",
            {"value": relative},
        )
    delta = int(
        (decimal_value * Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    conversion = {
        "base_logical_clock_ms": base_clock_ms,
        "relative_seconds": relative,
        "scale_ms_per_second": 1000,
        "rounding": "half_up",
        "rounding_target": "millisecond",
        "delta_ms": delta,
        "absolute_generated_ms": base_clock_ms + delta,
    }
    return base_clock_ms + delta, conversion


def load_v1(
    path: str | Path,
    *,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
    injected_clock_ms: int,
) -> Dict[str, Any]:
    raw = read_json_file(path)
    source = str(path)
    if not isinstance(raw, Mapping):
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT, "v1 快照必须是 JSON 对象", {"source": source}
        )
    if raw.get("format") != V1_FORMAT:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v1 快照 format 非法",
            {"source": source, "expected": V1_FORMAT, "actual": raw.get("format")},
        )
    clock = _require_nonnegative_int(
        raw.get("logical_clock_ms"), "logical_clock_ms", source=source
    )
    generation = _require_nonnegative_int(
        raw.get("plan_generation"), "plan_generation", source=source
    )
    relative = raw.get("generated_at_relative_seconds")
    absolute_ms, conversion = _convert_relative_ms(relative, injected_clock_ms)
    column_map = raw.get("column_map", {})
    plans_raw = raw.get("plans", {})
    if not isinstance(plans_raw, Mapping):
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v1 plans 必须是对象",
            {"source": source},
        )
    for name in plans_raw:
        if name not in views:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v1 计划引用了未声明视图",
                {"source": source, "view": name},
            )
    if not isinstance(column_map, Mapping):
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v1 column_map 必须是扁平对象",
            {"source": source},
        )
    columns_by_view: Dict[str, Dict[str, Any]] = {}
    for key, descriptor in column_map.items():
        if not isinstance(key, str) or key.count(".") != 1:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v1 column_map 键必须为 view.column",
                {"source": source, "key": key},
            )
        view_name, column_name = key.split(".", 1)
        if view_name not in records:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v1 column_map 引用了未声明视图",
                {"source": source, "view": view_name, "key": key},
            )
        columns_by_view.setdefault(view_name, {})[column_name] = descriptor

    all_view_names = sorted(set(plans_raw) | set(columns_by_view))
    candidates: List[SnapshotCandidate] = []
    sequence = 0
    for view_name in all_view_names:
        record = records[view_name]
        plan_meta = plans_raw.get(view_name, {})
        if plan_meta is None:
            plan_meta = {}
        if not isinstance(plan_meta, Mapping):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v1 plans 条目必须是对象",
                {"source": source, "view": view_name},
            )
        view_clock = _require_nonnegative_int(
            plan_meta.get("logical_clock_ms", clock),
            "logical_clock_ms",
            source=source,
            view=view_name,
        )
        view_generation = _require_nonnegative_int(
            plan_meta.get("plan_generation", generation),
            "plan_generation",
            source=source,
            view=view_name,
        )
        declared_digest = plan_meta.get("plan_digest")
        digest_matches = _validate_declared_plan(
            view_name, declared_digest, records, source="v1"
        )
        if view_name in columns_by_view:
            _validate_column_descriptors(
                {
                    f"{view_name}.{column_name}": descriptor
                    for column_name, descriptor in columns_by_view[view_name].items()
                },
                record,
                source="v1",
                flat_map=True,
            )
        candidates.append(
            SnapshotCandidate(
                view_name=view_name,
                source_version="v1",
                source_ref=source,
                logical_clock_ms=view_clock,
                plan_generation=view_generation,
                plan_digest=declared_digest or record.plan_digest,
                columns=record.columns,
                executable_sql=record.executable_sql,
                tombstone=False,
                original_time={
                    "kind": "relative_seconds",
                    "value": relative,
                    "absolute_generated_ms": absolute_ms,
                },
                time_conversion=conversion,
                sequence=sequence,
                digest_matches_current=(
                    digest_matches if declared_digest is not None else None
                ),
            )
        )
        sequence += 1
    return {
        "format": V1_FORMAT,
        "source": source,
        "snapshot_id": raw.get("snapshot_id"),
        "logical_clock_ms": clock,
        "plan_generation": generation,
        "generated_at_relative_seconds": relative,
        "generated_at_ms": absolute_ms,
        "time_conversion": conversion,
        "candidates": candidates,
        "column_map_entry_count": len(column_map),
    }


def _read_manifest(
    meta_path: str | Path,
    *,
    v2_dir: Path,
    shard_files: Sequence[Path],
) -> List[Dict[str, Any]]:
    source = str(meta_path)
    if not meta_path.exists():
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v2 元数据 SQLite 文件不存在",
            {"source": source},
        )
    connection: Optional[sqlite3.Connection] = None
    try:
        meta_uri = meta_path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(meta_uri, uri=True)
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "shard_manifest" not in tables:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2_meta.db 缺少 shard_manifest 表",
                {"source": source},
            )
        rows = [
            dict(row)
            for row in connection.execute("SELECT * FROM shard_manifest ORDER BY seq")
        ]
        tombstones: List[Dict[str, Any]] = []
        if "tombstones" in tables:
            tombstones = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM tombstones ORDER BY view_name, plan_generation"
                )
            ]
    except sqlite3.Error as exc:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "无法读取 v2_meta.db",
            {"source": source, "reason": str(exc)},
        ) from exc
    finally:
        if connection is not None:
            connection.close()

    if not rows:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v2 shard_manifest 不得为空",
            {"source": source},
        )
    manifest_files = set()
    last_generation = -1
    for index, row in enumerate(rows):
        file_name = row.get("shard_file")
        if not isinstance(file_name, str) or not file_name:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "shard_manifest.shard_file 非法",
                {"source": source, "row": index},
            )
        manifest_files.add(file_name)
        generation = _require_nonnegative_int(
            row.get("plan_generation"),
            "plan_generation",
            source=source,
        )
        if generation < last_generation:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2 shard 计划代数不是单调不减",
                {
                    "source": source,
                    "previous_generation": last_generation,
                    "current_generation": generation,
                    "shard_file": file_name,
                },
            )
        last_generation = generation
        _require_nonnegative_int(
            row.get("logical_clock_ms"), "logical_clock_ms", source=source
        )
        _require_nonnegative_int(
            row.get("generated_at_ms"), "generated_at_ms", source=source
        )
        declared_hash = row.get("sha256")
        if not isinstance(declared_hash, str) or not is_sha256(declared_hash):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "shard_manifest.sha256 非法",
                {"source": source, "shard_file": file_name},
            )
    actual_files = {path.name for path in shard_files}
    unlisted = sorted(actual_files - manifest_files)
    missing = sorted(manifest_files - actual_files)
    if unlisted or missing:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v2 分片文件与清单不一致",
            {
                "source": source,
                "unlisted": unlisted,
                "missing": missing,
            },
        )
    for row in tombstones:
        if not isinstance(row.get("view_name"), str):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2 tombstone 缺少 view_name",
                {"source": source},
            )
    if tombstones:
        rows_tombstones = tombstones
    else:
        rows_tombstones = []
    return rows + [{"__tombstone__": True, **row} for row in rows_tombstones]


def load_v2(
    directory: str | Path,
    *,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
    meta_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    v2_dir = Path(directory)
    if not v2_dir.is_dir():
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v2 快照目录不存在",
            {"source": str(v2_dir)},
        )
    if meta_path:
        meta = Path(meta_path)
    else:
        meta = v2_dir / "v2_meta.db"
        sibling_meta = v2_dir.parent / "v2_meta.db"
        if not meta.exists() and sibling_meta.exists():
            meta = sibling_meta
    shard_files = sorted(v2_dir.glob("*.json"))
    if not shard_files:
        raise BusinessError(
            ErrorCode.E_BAD_SNAPSHOT,
            "v2 快照目录没有 JSON 分片",
            {"source": str(v2_dir)},
        )
    manifest_rows = _read_manifest(meta, v2_dir=v2_dir, shard_files=shard_files)
    row_by_file = {
        row["shard_file"]: row for row in manifest_rows if not row.get("__tombstone__")
    }
    candidates: List[SnapshotCandidate] = []
    shard_audit: List[Dict[str, Any]] = []
    sequence = 0
    seen_views: Dict[str, SnapshotCandidate] = {}
    for shard_path in shard_files:
        row = row_by_file[shard_path.name]
        actual_hash = sha256_bytes(shard_path.read_bytes())
        if normalize_digest(actual_hash) != normalize_digest(row["sha256"]):
            raise BusinessError(
                ErrorCode.E_SHARD_HASH_MISMATCH,
                "v2 分片文件哈希不匹配",
                {
                    "source": str(shard_path),
                    "shard_file": shard_path.name,
                    "declared": row["sha256"],
                    "calculated": actual_hash,
                },
            )
        raw = read_json_file(shard_path)
        source = str(shard_path)
        if not isinstance(raw, Mapping) or raw.get("format") != V2_SHARD_FORMAT:
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2 分片 format 非法",
                {
                    "source": source,
                    "expected": V2_SHARD_FORMAT,
                    "actual": raw.get("format") if isinstance(raw, Mapping) else None,
                },
            )
        generation = _require_nonnegative_int(
            raw.get("plan_generation"), "plan_generation", source=source
        )
        clock = _require_nonnegative_int(
            raw.get("logical_clock_ms"), "logical_clock_ms", source=source
        )
        generated_ms = _require_nonnegative_int(
            raw.get("generated_at_ms"), "generated_at_ms", source=source
        )
        for field_name, raw_value in (
            ("plan_generation", generation),
            ("logical_clock_ms", clock),
            ("generated_at_ms", generated_ms),
        ):
            if int(row[field_name]) != raw_value:
                raise BusinessError(
                    ErrorCode.E_SNAPSHOT_META_MISMATCH,
                    "v2 分片与 SQLite 元数据不一致",
                    {
                        "source": source,
                        "field": field_name,
                        "shard_value": raw_value,
                        "meta_value": row[field_name],
                    },
                )
        entries = raw.get("views")
        if not isinstance(entries, list):
            raise BusinessError(
                ErrorCode.E_BAD_SNAPSHOT,
                "v2 分片 views 必须是数组",
                {"source": source},
            )
        if row.get("view_count") is not None and int(row["view_count"]) != len(entries):
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_META_MISMATCH,
                "v2 分片视图数量与元数据不一致",
                {
                    "source": source,
                    "shard_view_count": len(entries),
                    "meta_view_count": row["view_count"],
                },
            )
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v2 视图条目必须是对象",
                    {"source": source},
                )
            view_name = entry.get("name")
            if not isinstance(view_name, str):
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v2 视图条目缺少 name",
                    {"source": source},
                )
            tombstone_value = entry.get("tombstone", False)
            if not isinstance(tombstone_value, bool):
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v2 tombstone 必须是布尔值",
                    {
                        "source": source,
                        "view": view_name,
                        "value": tombstone_value,
                    },
                )
            tombstone = tombstone_value
            if not tombstone and view_name not in records:
                raise BusinessError(
                    ErrorCode.E_BAD_SNAPSHOT,
                    "v2 快照引用了未声明视图",
                    {"source": source, "view": view_name},
                )
            if tombstone and view_name not in views:
                # Historical tombstones remain auditable.
                pass
            candidate_clock = _require_nonnegative_int(
                entry.get("logical_clock_ms", clock),
                "logical_clock_ms",
                source=source,
                view=view_name,
            )
            candidate_generation = _require_nonnegative_int(
                entry.get("plan_generation", generation),
                "plan_generation",
                source=source,
                view=view_name,
            )
            if tombstone:
                candidate = SnapshotCandidate(
                    view_name=view_name,
                    source_version="v2",
                    source_ref=source,
                    logical_clock_ms=candidate_clock,
                    plan_generation=candidate_generation,
                    plan_digest=None,
                    columns=[],
                    executable_sql=None,
                    tombstone=True,
                    original_time={"kind": "absolute_ms", "value": generated_ms},
                    sequence=sequence,
                    reason=entry.get("reason"),
                )
            else:
                record = records[view_name]
                digest_matches = _validate_declared_plan(
                    view_name, entry.get("plan_digest"), records, source="v2"
                )
                _validate_column_descriptors(entry.get("columns"), record, source="v2")
                candidate = SnapshotCandidate(
                    view_name=view_name,
                    source_version="v2",
                    source_ref=source,
                    logical_clock_ms=candidate_clock,
                    plan_generation=candidate_generation,
                    plan_digest=entry.get("plan_digest") or record.plan_digest,
                    columns=record.columns,
                    executable_sql=entry.get("executable_sql"),
                    tombstone=False,
                    original_time={"kind": "absolute_ms", "value": generated_ms},
                    sequence=sequence,
                    digest_matches_current=(
                        digest_matches if entry.get("plan_digest") is not None else None
                    ),
                )
            previous = seen_views.get(view_name)
            if previous is None or candidate.sort_key() > previous.sort_key():
                if previous is not None:
                    previous.reason = "superseded_within_v2"
                    candidate.reason = (
                        f"superseded_v2:{previous.source_ref}:{previous.sequence}"
                    )
                seen_views[view_name] = candidate
            else:
                candidate.reason = (
                    f"superseded_v2:{seen_views[view_name].source_ref}:"
                    f"{seen_views[view_name].sequence}"
                )
            candidates.append(candidate)
            sequence += 1
        shard_audit.append(
            {
                "shard_file": shard_path.name,
                "sha256": actual_hash,
                "plan_generation": generation,
                "logical_clock_ms": clock,
                "generated_at_ms": generated_ms,
                "view_count": len(entries),
            }
        )

    for row in manifest_rows:
        if not row.get("__tombstone__"):
            continue
        view_name = row["view_name"]
        generation = _require_nonnegative_int(
            row.get("plan_generation"), "plan_generation", source=str(meta)
        )
        clock = _require_nonnegative_int(
            row.get("logical_clock_ms"), "logical_clock_ms", source=str(meta)
        )
        generated_ms = _require_nonnegative_int(
            row.get("generated_at_ms"), "generated_at_ms", source=str(meta)
        )
        candidate = SnapshotCandidate(
            view_name=view_name,
            source_version="v2",
            source_ref=str(meta) + "#tombstones",
            logical_clock_ms=clock,
            plan_generation=generation,
            plan_digest=None,
            columns=[],
            executable_sql=None,
            tombstone=True,
            original_time={"kind": "absolute_ms", "value": generated_ms},
            sequence=sequence,
            reason=row.get("reason"),
        )
        candidates.append(candidate)
        sequence += 1

    final_generation = max(
        [
            int(row["plan_generation"])
            for row in manifest_rows
            if not row.get("__tombstone__")
        ]
        + [0]
    )
    return {
        "format": "sqlplan/snapshot/v2-package",
        "source": str(v2_dir),
        "meta": str(meta),
        "plan_generation": final_generation,
        "shards": shard_audit,
        "candidates": candidates,
    }


def _rejection_reason(candidate: SnapshotCandidate, winner: SnapshotCandidate) -> str:
    if candidate.logical_clock_ms < winner.logical_clock_ms:
        return "older_logical_clock"
    if candidate.logical_clock_ms > winner.logical_clock_ms:
        return "unexpected"
    if candidate.plan_generation < winner.plan_generation:
        return "lower_plan_generation"
    if candidate.plan_generation > winner.plan_generation:
        return "unexpected"
    priority = {"v1": 1, "v2": 2}
    if priority[candidate.source_version] < priority[winner.source_version]:
        return "lower_source_format_priority"
    return candidate.reason or "equivalent_entry_superseded"


def merge_snapshots(
    *,
    views: Mapping[str, ViewDef],
    v1: Mapping[str, Any],
    v2: Mapping[str, Any],
    view_watermark: int,
) -> Tuple[Dict[str, Any], int, int, str]:
    all_candidates: List[SnapshotCandidate] = list(v1["candidates"]) + list(
        v2["candidates"]
    )
    by_view: Dict[str, List[SnapshotCandidate]] = {}
    for candidate in all_candidates:
        by_view.setdefault(candidate.view_name, []).append(candidate)

    merged_views: Dict[str, Any] = {}
    for view_name in sorted(by_view):
        candidates = by_view[view_name]
        winner = max(candidates, key=lambda item: item.sort_key())
        rejected = []
        for candidate in sorted(
            candidates, key=lambda item: item.sort_key(), reverse=True
        ):
            if candidate is winner:
                continue
            rejected.append(
                candidate.audit(reason=_rejection_reason(candidate, winner))
            )
        merged_views[view_name] = {
            "view": view_name,
            "winner": winner.audit(winner=True),
            "decision_basis": [
                "logical_clock_ms",
                "plan_generation",
                "source_format_priority(v2>v1)",
            ],
            "tombstone": winner.tombstone,
            "snapshot_cache_verdict": (
                "recompute_stale"
                if winner.digest_matches_current is False
                else (
                    "reuse_verified"
                    if winner.digest_matches_current is True
                    else "recompute_undeclared_digest"
                )
            ),
            "requires_recompute": winner.digest_matches_current is not True,
            "rejected": rejected,
            "in_unified_plan": (not winner.tombstone and view_name in views),
        }
    source_generation = max(
        [0]
        + [candidate.plan_generation for candidate in all_candidates]
        + [int(v1.get("plan_generation") or 0), int(v2.get("plan_generation") or 0)]
    )
    snapshot_watermark = max(
        [view_watermark]
        + [int(v1.get("logical_clock_ms") or 0)]
        + [candidate.logical_clock_ms for candidate in all_candidates]
        + [int(shard.get("logical_clock_ms") or 0) for shard in v2.get("shards", [])]
    )
    merged = {
        "format": "sqlplan/merged-snapshot/v1",
        "source_plan_generation": source_generation,
        "logical_clock_watermark": snapshot_watermark,
        "v1_time_conversion": v1["time_conversion"],
        "views": merged_views,
    }
    semantic_content = {
        "source_plan_generation": source_generation,
        "logical_clock_watermark": snapshot_watermark,
        "v1_time_conversion": v1["time_conversion"],
        "views": {
            name: {
                "winner": {
                    "source_version": entry["winner"]["source_version"],
                    "logical_clock_ms": entry["winner"]["logical_clock_ms"],
                    "plan_generation": entry["winner"]["plan_generation"],
                    "plan_digest": entry["winner"]["plan_digest"],
                    "tombstone": entry["winner"]["tombstone"],
                    "original_time": entry["winner"]["original_time"],
                },
                "rejected": [
                    {
                        "source_version": rejected["source_version"],
                        "logical_clock_ms": rejected["logical_clock_ms"],
                        "plan_generation": rejected["plan_generation"],
                        "plan_digest": rejected["plan_digest"],
                        "tombstone": rejected["tombstone"],
                        "original_time": rejected["original_time"],
                        "rejected_reason": rejected.get("rejected_reason"),
                    }
                    for rejected in entry["rejected"]
                ],
            }
            for name, entry in sorted(merged_views.items())
        },
    }
    content_digest = "sha256:" + digest_object(semantic_content)
    merged["content_digest"] = content_digest
    return merged, source_generation, snapshot_watermark, content_digest


def load_snapshot_bundle(
    *,
    views: Mapping[str, ViewDef],
    records: Mapping[str, PlanRecord],
    v1_path: str | Path,
    v2_dir: str | Path,
    v2_meta_path: Optional[str | Path] = None,
    view_watermark: int,
    injected_clock_ms: Optional[int] = None,
) -> SnapshotBundle:
    base_clock = injected_clock_ms if injected_clock_ms is not None else view_watermark
    if not _is_nonnegative_int(base_clock):
        raise BusinessError(
            ErrorCode.E_BAD_CLOCK,
            "注入逻辑时钟必须是非负整数",
            {"value": base_clock},
        )
    v1 = load_v1(
        v1_path,
        views=views,
        records=records,
        injected_clock_ms=base_clock,
    )
    v2 = load_v2(
        v2_dir,
        views=views,
        records=records,
        meta_path=v2_meta_path,
    )
    merged, generation, watermark, digest = merge_snapshots(
        views=views,
        v1=v1,
        v2=v2,
        view_watermark=view_watermark,
    )
    v1.pop("candidates", None)
    v2.pop("candidates", None)
    return SnapshotBundle(
        v1=v1,
        v2=v2,
        merged=merged,
        source_plan_generation=generation,
        logical_clock_watermark=watermark,
        content_digest=digest,
    )
