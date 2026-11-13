"""JSON 运行报告：生成、aborted 临时报告、恢复溯源与 Schema 校验。

不引入第三方 jsonschema 依赖（任务仅允许 requests/pytest），
内置覆盖本报告结构的 JSON Schema 草案与轻量校验器。
"""
from __future__ import annotations

import glob
import json
import os

from .storage import utc_now_iso

REPORT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ReconRunReport",
    "type": "object",
    "required": ["run_id", "run_status", "started_at", "finished_at",
                 "sources", "resumed_from_run_id", "resume_count"],
    "properties": {
        "run_id": {"type": "string", "minLength": 1},
        "run_status": {"type": "string",
                       "enum": ["success", "partial", "failed", "aborted"]},
        "started_at": {"type": "string", "minLength": 1},
        "finished_at": {"type": "string", "minLength": 1},
        "resumed_from_run_id": {"type": ["string", "null"]},
        "resume_count": {"type": "integer", "minimum": 0},
        "resume_detail": {"type": ["object", "null"]},
        "sources": {
            "type": "object",
            "required": ["rest", "ts", "csv"],
            "properties": {
                "rest": {"$ref": "#/$defs/source"},
                "ts": {"$ref": "#/$defs/source"},
                "csv": {"$ref": "#/$defs/source"},
            },
        },
    },
    "$defs": {
        "source": {
            "type": "object",
            "required": ["status", "fetched", "duplicate_dropped", "retry_count",
                         "cursor", "committed_batches"],
            "properties": {
                "status": {"type": "string",
                           "enum": ["success", "failed", "aborted", "skipped"]},
                "fetched": {"type": "integer", "minimum": 0},
                "duplicate_dropped": {"type": "integer", "minimum": 0},
                "retry_count": {"type": "integer", "minimum": 0},
                "cursor": {"type": ["string", "number", "object", "null"]},
                "cursor_type": {"type": "string"},
                "committed_batches": {"type": "integer", "minimum": 0},
                "error_code": {"type": ["string", "null"]},
                "circuit_open": {"type": "boolean"},
                "first_cursor_after_resume": {"type": ["string", "number", "object", "null"]},
            },
        }
    },
}


def new_source_report(cursor_type: str | None = None) -> dict:
    return {
        "status": "skipped",
        "fetched": 0,
        "duplicate_dropped": 0,
        "retry_count": 0,
        "cursor": None,
        "cursor_type": cursor_type,
        "committed_batches": 0,
        "error_code": None,
        "circuit_open": False,
        "first_cursor_after_resume": None,
    }


def build_report(run_id: str, run_status: str, started_at: str,
                 sources: dict, resumed_from_run_id: str | None,
                 resume_count: int, resume_detail: dict | None) -> dict:
    return {
        "run_id": run_id,
        "run_status": run_status,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "resumed_from_run_id": resumed_from_run_id,
        "resume_count": resume_count,
        "resume_detail": resume_detail,
        "sources": sources,
    }


def report_path(report_dir: str, run_id: str, aborted: bool = False) -> str:
    suffix = ".aborted" if aborted else ""
    return os.path.join(report_dir, f"report_{run_id}{suffix}.json")


def write_report(report_dir: str, report: dict, aborted: bool = False) -> str:
    os.makedirs(report_dir, exist_ok=True)
    path = report_path(report_dir, report["run_id"], aborted)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def find_latest_aborted(report_dir: str) -> dict | None:
    pattern = os.path.join(report_dir, "report_*.aborted.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    latest = max(candidates, key=os.path.getmtime)
    with open(latest, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------- mini validator

def _type_ok(value, type_spec) -> bool:
    if isinstance(type_spec, list):
        return any(_type_ok(value, t) for t in type_spec)
    if type_spec == "string":
        return isinstance(value, str)
    if type_spec == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_spec == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_spec == "boolean":
        return isinstance(value, bool)
    if type_spec == "object":
        return isinstance(value, dict)
    if type_spec == "null":
        return value is None
    return True


def validate_against_schema(instance, schema: dict, defs: dict | None = None,
                            path: str = "$") -> list[str]:
    errors: list[str] = []
    defs = defs or schema.get("$defs", {})
    if "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        schema = defs[ref]
    expected = schema.get("type")
    if expected and not _type_ok(instance, expected):
        errors.append(f"{path}: 期望类型 {expected}，实际 {type(instance).__name__}")
        return errors
    if expected == "object":
        for req in schema.get("required", []):
            if req not in instance:
                errors.append(f"{path}: 缺少必填字段 {req}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in instance:
                errors.extend(validate_against_schema(
                    instance[key], sub, defs, f"{path}.{key}"))
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: 值 {instance!r} 不在枚举 {schema['enum']} 内")
    if "minLength" in schema and isinstance(instance, str) \
            and len(instance) < schema["minLength"]:
        errors.append(f"{path}: 字符串短于 minLength={schema['minLength']}")
    if "minimum" in schema and isinstance(instance, (int, float)) \
            and not isinstance(instance, bool) and instance < schema["minimum"]:
        errors.append(f"{path}: 小于 minimum={schema['minimum']}")
    return errors


def validate_report(report: dict) -> list[str]:
    errors = validate_against_schema(report, REPORT_SCHEMA)
    if errors:
        return errors
    # 额外业务约束
    for name, src in report["sources"].items():
        if src["status"] == "failed" and not src["error_code"]:
            errors.append(f"$.sources.{name}: failed 状态必须带 error_code")
    return errors
