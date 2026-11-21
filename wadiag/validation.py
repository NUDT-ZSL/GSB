from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .errors import EngineError, error
from .models import (
    ClockTable,
    DiagnosticConfig,
    StorageConfig,
    Trajectory,
    WriteRecord,
)
from .serialization import read_json


KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
WRITE_OPS = frozenset({"put", "write", "upsert"})
DELETE_OPS = frozenset({"delete", "del", "tombstone"})
ALL_OPS = WRITE_OPS | DELETE_OPS


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_mapping(value: Any, code: str, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error(code, f"{what}必须是 JSON 对象", expected="object", actual=type(value).__name__)
    return value


def _require_int(
    mapping: dict[str, Any],
    field_name: str,
    code: str,
    *,
    minimum: int | None = None,
    positive: bool = False,
) -> int:
    if field_name not in mapping:
        raise error(code, f"缺少字段 {field_name}", field=field_name)
    value = mapping[field_name]
    if not _is_int(value):
        raise error(
            code,
            f"{field_name} 必须为整数",
            field=field_name,
            actual=repr(value),
        )
    if positive and value <= 0:
        raise error(code, f"{field_name} 必须为正整数", field=field_name, actual=value)
    if minimum is not None and value < minimum:
        raise error(
            code,
            f"{field_name} 不得小于 {minimum}",
            field=field_name,
            actual=value,
        )
    return value


def _decimal_from(value: Any, code: str, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise error(code, f"{field_name} 必须为有限十进制数", field=field_name, actual=repr(value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise error(code, f"{field_name} 不得为 NaN 或无穷", field=field_name)
        text = repr(value)
    elif isinstance(value, (int, str)):
        text = str(value)
    else:
        raise error(
            code,
            f"{field_name} 必须为十进制数或十进制字符串",
            field=field_name,
            actual=type(value).__name__,
        )
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise error(code, f"{field_name} 无法解析为十进制数", field=field_name, actual=text) from exc
    if not parsed.is_finite():
        raise error(code, f"{field_name} 不得为 NaN 或无穷", field=field_name, actual=text)
    return parsed


def _normalize_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." not in normalized:
        normalized += ".0"
    return normalized


def parse_trajectory(payload: Any) -> Trajectory:
    code = "E_BAD_TRAJECTORY"
    if isinstance(payload, list):
        raw_records = payload
        schema_version = 1
        trajectory_id = "inline"
    else:
        mapping = _require_mapping(payload, code, "写入轨迹")
        schema_version = mapping.get("schema_version", 1)
        if schema_version != 1:
            raise error(code, "不支持的轨迹 schema_version", actual=schema_version)
        raw_records = mapping.get("records")
        if raw_records is None and "trajectory" in mapping:
            raw_records = mapping["trajectory"]
        trajectory_id = mapping.get("trajectory_id", "default")
        if not isinstance(trajectory_id, str) or not trajectory_id or len(trajectory_id) > 128:
            raise error(code, "trajectory_id 非法", actual=repr(trajectory_id))
    if not isinstance(raw_records, list):
        raise error(code, "records 必须为数组", actual=type(raw_records).__name__)
    if not raw_records:
        raise error(code, "records 不得为空")

    records: list[WriteRecord] = []
    previous_seq: int | None = None
    seen: set[int] = set()
    for index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            raise error(code, "轨迹记录必须为对象", index=index)
        seq = _require_int(item, "seq", code, minimum=0)
        if previous_seq is not None and seq <= previous_seq:
            raise error(
                code,
                "记录序号必须严格递增且唯一",
                index=index,
                previous=previous_seq,
                actual=seq,
            )
        if seq in seen:
            raise error(code, "记录序号重复", index=index, seq=seq)
        key = item.get("key")
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise error(
                code,
                "键格式非法",
                index=index,
                seq=seq,
                actual=repr(key),
                pattern=KEY_PATTERN.pattern,
            )
        value_bytes = _require_int(item, "value_bytes", code, minimum=0)
        op = item.get("op")
        if not isinstance(op, str) or op not in ALL_OPS:
            raise error(
                code,
                "操作类型非法",
                index=index,
                seq=seq,
                actual=repr(op),
                allowed=sorted(ALL_OPS),
            )
        clock_ms = _require_int(item, "clock_ms", code, minimum=0)
        canonical_op = "delete" if op in DELETE_OPS else "put"
        records.append(
            WriteRecord(
                seq=seq,
                key=key,
                value_bytes=value_bytes,
                op=canonical_op,
                clock_ms=clock_ms,
            )
        )
        previous_seq = seq
        seen.add(seq)
    return Trajectory(
        schema_version=int(schema_version),
        trajectory_id=trajectory_id,
        records=tuple(records),
    )


def parse_storage_config(payload: Any) -> StorageConfig:
    code = "E_BAD_STORAGE"
    mapping = _require_mapping(payload, code, "存储配置")
    schema_version = mapping.get("schema_version", 1)
    if schema_version != 1:
        raise error(code, "不支持的存储配置 schema_version", actual=schema_version)
    memtable = _require_int(
        mapping, "memtable_capacity_bytes", code, positive=True
    )
    segment_limit = _require_int(
        mapping, "segment_size_limit_bytes", code, positive=True
    )
    level_count = _require_int(mapping, "level_count", code, positive=True)
    thresholds_raw = mapping.get("level_trigger_thresholds")
    if not isinstance(thresholds_raw, list):
        raise error("E_BAD_LEVELS", "level_trigger_thresholds 必须为数组")
    if len(thresholds_raw) != level_count:
        raise error(
            "E_BAD_LEVELS",
            "每层触发阈值数量必须等于合并层级数",
            expected=level_count,
            actual=len(thresholds_raw),
        )
    thresholds: list[int] = []
    for index, value in enumerate(thresholds_raw):
        if not _is_int(value) or value <= 0:
            raise error(
                "E_BAD_LEVELS",
                "每层触发阈值必须为正整数",
                level=index,
                actual=repr(value),
            )
        thresholds.append(value)
    retention = _require_int(
        mapping, "tombstone_retention_generations", code, minimum=0
    )
    block_size = _require_int(
        mapping, "compression_block_size_bytes", code, positive=True
    )
    budget = _require_int(mapping, "merge_budget_bytes", code, positive=True)
    clock_step = _require_int(mapping, "logical_clock_step_ms", code, minimum=0)
    bloom_bits = mapping.get("bloom_bits_per_key", 10)
    if not _is_int(bloom_bits) or bloom_bits < 1:
        raise error(code, "bloom_bits_per_key 必须为正整数", actual=repr(bloom_bits))
    bloom_hashes = mapping.get("bloom_hash_count", 3)
    if not _is_int(bloom_hashes) or bloom_hashes < 1:
        raise error(code, "bloom_hash_count 必须为正整数", actual=repr(bloom_hashes))

    ratios_raw = mapping.get("level_compression_ratios")
    if ratios_raw is None:
        ratios = [max(Decimal("0.50"), Decimal("0.95") - Decimal(level) * Decimal("0.05")) for level in range(level_count)]
    else:
        if not isinstance(ratios_raw, list) or len(ratios_raw) != level_count:
            raise error(
                code,
                "level_compression_ratios 数量必须等于层级数",
                expected=level_count,
            )
        ratios = []
        for index, value in enumerate(ratios_raw):
            parsed = _decimal_from(value, code, f"level_compression_ratios[{index}]")
            if parsed <= 0:
                raise error(code, "压缩比必须为正数", level=index, actual=str(parsed))
            ratios.append(parsed)
    return StorageConfig(
        schema_version=int(schema_version),
        memtable_capacity_bytes=memtable,
        segment_size_limit_bytes=segment_limit,
        level_count=level_count,
        level_trigger_thresholds=tuple(thresholds),
        tombstone_retention_generations=retention,
        compression_block_size_bytes=block_size,
        merge_budget_bytes=budget,
        logical_clock_step_ms=clock_step,
        level_compression_ratios=tuple(_normalize_decimal(value) for value in ratios),
        bloom_bits_per_key=bloom_bits,
        bloom_hash_count=bloom_hashes,
    )


def parse_diagnostic_config(payload: Any) -> DiagnosticConfig:
    code = "E_BAD_CONFIG"
    mapping = _require_mapping(payload, code, "诊断配置")
    schema_version = mapping.get("schema_version", 1)
    if schema_version != 1:
        raise error(code, "不支持的诊断配置 schema_version", actual=schema_version)
    fixed_scale = _require_int(mapping, "fixed_scale", "E_BAD_SCALE", minimum=0)
    if fixed_scale > 12:
        raise error("E_BAD_SCALE", "fixed_scale 不得大于 12", actual=fixed_scale)
    tolerance = _decimal_from(mapping.get("float_tolerance"), "E_BAD_TOLERANCE", "float_tolerance")
    if tolerance <= 0:
        raise error("E_BAD_TOLERANCE", "float_tolerance 必须大于零", actual=str(tolerance))
    error_budget = _decimal_from(mapping.get("error_budget"), "E_BAD_BUDGET", "error_budget")
    if error_budget < 0:
        raise error("E_BAD_BUDGET", "error_budget 不得为负", actual=str(error_budget))
    sample_ratio = _decimal_from(mapping.get("sample_ratio"), "E_BAD_SAMPLE", "sample_ratio")
    if sample_ratio <= 0 or sample_ratio > 1:
        raise error(
            "E_BAD_SAMPLE",
            "sample_ratio 必须位于 (0, 1]",
            actual=str(sample_ratio),
        )
    return DiagnosticConfig(
        schema_version=int(schema_version),
        fixed_scale=fixed_scale,
        float_tolerance=_normalize_decimal(tolerance),
        error_budget=_normalize_decimal(error_budget),
        sample_ratio=_normalize_decimal(sample_ratio),
    )


def parse_clock_table(payload: Any | None) -> ClockTable:
    if payload is None:
        return ClockTable(schema_version=1, start_ms=0, step_ms=1, overrides={})
    code = "E_BAD_CLOCK"
    mapping = _require_mapping(payload, code, "逻辑时钟步进表")
    schema_version = mapping.get("schema_version", 1)
    if schema_version != 1:
        raise error(code, "不支持的时钟 schema_version", actual=schema_version)
    start_ms = _require_int(mapping, "start_ms", code, minimum=0)
    step_ms = _require_int(mapping, "step_ms", code, minimum=0)
    overrides_raw = mapping.get("overrides", {})
    if not isinstance(overrides_raw, dict):
        raise error(code, "overrides 必须为对象")
    overrides: dict[int, int] = {}
    for raw_seq, raw_clock in overrides_raw.items():
        try:
            seq = int(raw_seq)
        except (TypeError, ValueError) as exc:
            raise error(code, "时钟 override 序号非法", actual=repr(raw_seq)) from exc
        if seq < 0 or not _is_int(raw_clock) or raw_clock < 0:
            raise error(
                code,
                "时钟 override 必须使用非负整数",
                seq=seq,
                actual=repr(raw_clock),
            )
        overrides[seq] = raw_clock
    return ClockTable(
        schema_version=int(schema_version),
        start_ms=start_ms,
        step_ms=step_ms,
        overrides=overrides,
    )


def load_trajectory(path: str | Path) -> Trajectory:
    return parse_trajectory(read_json(path, "E_BAD_TRAJECTORY"))


def load_storage_config(path: str | Path) -> StorageConfig:
    return parse_storage_config(read_json(path, "E_BAD_STORAGE"))


def load_diagnostic_config(path: str | Path) -> DiagnosticConfig:
    return parse_diagnostic_config(read_json(path, "E_BAD_CONFIG"))


def load_clock_table(path: str | Path | None) -> ClockTable:
    if path is None:
        return parse_clock_table(None)
    return parse_clock_table(read_json(path, "E_BAD_CLOCK"))
