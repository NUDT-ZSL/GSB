from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .db import advance_clock, immediate
from .errors import BizError, ErrorCode
from .hashing import canonical_digest, is_digest
from .tensors import (
    DTYPES,
    block_count,
    is_finite_positive_number,
    is_non_negative_int,
    is_positive_int,
    validate_block_range,
    verify_descriptor_digest,
)


def read_jsonl_or_array(
    path: Path, error_code: str = ErrorCode.MANIFEST_INVALID
) -> List[Dict[str, Any]]:
    if not path.exists():
        raise BizError(
            ErrorCode.MANIFEST_INVALID, "required input file does not exist", {"path": str(path)}
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix == ".jsonl":
        rows = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BizError(
                    error_code,
                    "JSONL line is malformed",
                    {"path": str(path), "line": line_no, "column": exc.colno},
                ) from exc
            if not isinstance(row, dict):
                raise BizError(
                    error_code,
                    "JSONL row must be an object",
                    {"path": str(path), "line": line_no},
                )
            rows.append(row)
        return rows
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BizError(
            error_code,
            "JSON file is malformed",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if isinstance(data, dict) and isinstance(data.get("tasks") or data.get("patches"), list):
        return data.get("tasks") or data.get("patches")
    if isinstance(data, list):
        return data
    raise BizError(ErrorCode.MANIFEST_INVALID, "JSON input must be a list or wrapper object")


def validate_task(row: Dict[str, Any], index: int) -> Dict[str, Any]:
    case_id = row.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise BizError(
            ErrorCode.MANIFEST_INVALID, "case_id must be a non-empty string", {"index": index}
        )
    if not is_positive_int(row.get("data_size")):
        raise BizError(
            ErrorCode.DATA_SIZE_INVALID,
            "data_size must be a positive integer",
            {"case_id": case_id, "data_size": row.get("data_size")},
        )
    if not is_positive_int(row.get("feature_dim")):
        raise BizError(
            ErrorCode.FEATURE_DIM_INVALID,
            "feature_dim must be a positive integer",
            {"case_id": case_id, "feature_dim": row.get("feature_dim")},
        )
    if not is_positive_int(row.get("label_dim")):
        raise BizError(
            ErrorCode.LABEL_DIM_INVALID,
            "label_dim must be a positive integer",
            {"case_id": case_id, "label_dim": row.get("label_dim")},
        )
    if row.get("dtype") not in DTYPES:
        raise BizError(
            ErrorCode.DTYPE_UNSUPPORTED,
            "dtype is not in the supported enum",
            {"case_id": case_id, "dtype": row.get("dtype"), "supported": sorted(DTYPES)},
        )
    if not is_finite_positive_number(row.get("quant_scale")):
        raise BizError(
            ErrorCode.QUANT_PARAM_INVALID,
            "quant_scale must be a finite positive number",
            {"case_id": case_id, "quant_scale": row.get("quant_scale")},
        )
    if not is_finite_positive_number(row.get("quant_zero_point")):
        raise BizError(
            ErrorCode.QUANT_PARAM_INVALID,
            "quant_zero_point must be a finite positive number",
            {"case_id": case_id, "quant_zero_point": row.get("quant_zero_point")},
        )
    plan = row.get("block_plan")
    if not isinstance(plan, dict):
        raise BizError(
            ErrorCode.BLOCK_PLAN_INVALID,
            "block_plan must be an object",
            {"case_id": case_id, "block_plan": plan},
        )
    block_count(int(row["data_size"]), plan)
    revision = row.get("revision", 1)
    revision_valid = is_non_negative_int(revision) or (
        isinstance(revision, str) and bool(revision.strip())
    )
    if not revision_valid:
        raise BizError(
            ErrorCode.MANIFEST_INVALID,
            "revision must be a non-negative integer or non-empty string",
            {"case_id": case_id, "revision": revision},
        )
    clock = row.get("logical_clock_ms", 0)
    if not is_non_negative_int(clock):
        raise BizError(
            ErrorCode.MANIFEST_INVALID,
            "logical_clock_ms must be a non-negative integer",
            {"case_id": case_id, "logical_clock_ms": clock},
        )
    return row


def load_manifest(data_dir: Path) -> List[Dict[str, Any]]:
    candidates = [data_dir / "tasks.jsonl", data_dir / "tasks.json", data_dir / "manifest.json"]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        raise BizError(
            ErrorCode.MANIFEST_INVALID,
            "no task manifest found; expected tasks.jsonl or tasks.json",
            {"data_dir": str(data_dir)},
        )
    rows = read_jsonl_or_array(path, ErrorCode.PATCH_INVALID)
    seen = set()
    validated = []
    for index, row in enumerate(rows):
        task = validate_task(row, index)
        if task["case_id"] in seen:
            raise BizError(
                ErrorCode.CASE_ID_DUPLICATE,
                "case_id must be unique",
                {"case_id": task["case_id"], "index": index},
            )
        seen.add(task["case_id"])
        validated.append(task)
    if not validated:
        raise BizError(ErrorCode.MANIFEST_INVALID, "task manifest is empty", {"path": str(path)})
    return validated


def load_and_validate_samples(
    data_dir: Path, tasks: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    by_id = {task["case_id"]: task for task in tasks}
    result: Dict[str, List[Dict[str, Any]]] = {}
    sample_dir = data_dir / "samples"
    for case_id, task in by_id.items():
        path = sample_dir / f"{case_id}.json"
        if not path.exists():
            matching = data_dir / f"{case_id}.json"
            if matching.exists():
                path = matching
        if not path.exists():
            raise BizError(
                ErrorCode.SAMPLE_INVALID,
                "sample tensor description is missing",
                {"case_id": case_id, "expected_path": str(sample_dir / f'{case_id}.json')},
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise BizError(
                ErrorCode.SAMPLE_INVALID,
                "sample JSON is malformed",
                {"case_id": case_id, "path": str(path), "line": exc.lineno},
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("tensors"), list):
            raise BizError(
                ErrorCode.SAMPLE_INVALID,
                "sample file must contain a tensors list",
                {"case_id": case_id, "path": str(path)},
            )
        if raw.get("case_id") not in (None, case_id):
            raise BizError(
                ErrorCode.SAMPLE_INVALID,
                "sample case_id does not match file/manifest",
                {"file_case_id": raw.get("case_id"), "case_id": case_id},
            )
        descriptors = []
        count = block_count(task["data_size"], task["block_plan"])
        seen_keys = set()
        for descriptor in raw["tensors"]:
            if not isinstance(descriptor, dict):
                raise BizError(ErrorCode.SAMPLE_INVALID, "tensor descriptor must be object")
            if descriptor.get("case_id") not in (None, case_id):
                raise BizError(ErrorCode.SAMPLE_CASE_NOT_FOUND, "descriptor case_id mismatch")
            role = descriptor.get("role")
            block_index = descriptor.get("block_index")
            name = descriptor.get("name")
            if role not in {"feature", "label"}:
                raise BizError(
                    ErrorCode.SAMPLE_INVALID,
                    "tensor role must be feature or label",
                    {"case_id": case_id, "role": role},
                )
            if not is_non_negative_int(block_index) or block_index >= count:
                raise BizError(
                    ErrorCode.TENSOR_SHAPE_INVALID,
                    "tensor block_index is outside block plan",
                    {"case_id": case_id, "block_index": block_index, "block_count": count},
                )
            key = (role, block_index, name)
            if key in seen_keys:
                raise BizError(
                    ErrorCode.SAMPLE_INVALID,
                    "duplicate tensor descriptor key",
                    {"case_id": case_id, "key": key},
                )
            seen_keys.add(key)
            shape = descriptor.get("shape")
            if not isinstance(shape, list) or not shape:
                raise BizError(ErrorCode.TENSOR_SHAPE_INVALID, "shape must be non-empty list")
            from .tensors import block_bounds

            start, end = block_bounds(task["data_size"], task["block_plan"], block_index)
            expected_rows = end - start
            expected_dim = task["feature_dim"] if role == "feature" else task["label_dim"]
            if shape[0] != expected_rows or (len(shape) >= 2 and shape[1] != expected_dim):
                raise BizError(
                    ErrorCode.TENSOR_SHAPE_INVALID,
                    "tensor shape does not match block bounds or manifest dimension",
                    {
                        "case_id": case_id,
                        "role": role,
                        "block_index": block_index,
                        "shape": shape,
                        "expected_first_dim": expected_rows,
                        "expected_second_dim": expected_dim,
                    },
                )
            if descriptor.get("dtype") not in DTYPES:
                raise BizError(
                    ErrorCode.DTYPE_UNSUPPORTED,
                    "tensor dtype unsupported",
                    {"case_id": case_id, "dtype": descriptor.get("dtype")},
                )
            verify_descriptor_digest(descriptor)
            descriptors.append(descriptor)
        result[case_id] = descriptors
    return result


def validate_patch(row: Dict[str, Any], tasks_by_id: Dict[str, Dict[str, Any]], index: int) -> Dict[str, Any]:
    patch_id = row.get("patch_id")
    if not isinstance(patch_id, str) or not patch_id:
        raise BizError(ErrorCode.PATCH_INVALID, "patch_id must be non-empty string", {"index": index})
    case_id = row.get("case_id")
    if case_id not in tasks_by_id:
        raise BizError(
            ErrorCode.SAMPLE_CASE_NOT_FOUND,
            "patch references unknown case_id",
            {"patch_id": patch_id, "case_id": case_id},
        )
    clock = row.get("logical_clock_ms")
    if not is_non_negative_int(clock):
        raise BizError(
            ErrorCode.PATCH_CLOCK_INVALID,
            "patch logical_clock_ms must be a non-negative integer",
            {"patch_id": patch_id, "logical_clock_ms": clock},
        )
    generation = row.get("patch_generation", 1)
    if not is_positive_int(generation):
        raise BizError(
            ErrorCode.PATCH_INVALID,
            "patch_generation must be a positive integer",
            {"patch_id": patch_id, "patch_generation": generation},
        )
    patch_range = row.get("block_range")
    if patch_range is None:
        patch_range = [row.get("block_start"), row.get("block_end")]
    if not isinstance(patch_range, list) or len(patch_range) != 2:
        raise BizError(
            ErrorCode.BLOCK_RANGE_OUT_OF_BOUNDS,
            "patch block_range must contain two integers",
            {"patch_id": patch_id, "block_range": patch_range},
        )
    task = tasks_by_id[case_id]
    start, end, count = validate_block_range(
        case_id, task["data_size"], task["block_plan"], patch_range[0], patch_range[1]
    )
    digest = row.get("new_digest")
    if not is_digest(digest):
        raise BizError(
            ErrorCode.DIGEST_MISMATCH,
            "patch new_digest must be a sha256 digest",
            {"patch_id": patch_id, "new_digest": digest},
        )
    content = row.get("patch_content")
    if isinstance(content, dict):
        actual = canonical_digest(content)
        if actual != digest:
            raise BizError(
                ErrorCode.DIGEST_MISMATCH,
                "patch new_digest does not match patch_content",
                {"patch_id": patch_id, "expected": digest, "actual": actual},
            )
    kind = row.get("patch_kind")
    if kind not in {"weight_update", "activation_recompute"}:
        raise BizError(
            ErrorCode.PATCH_INVALID,
            "patch_kind must be weight_update or activation_recompute",
            {"patch_id": patch_id, "patch_kind": kind},
        )
    refs = row.get("refs", [])
    if not isinstance(refs, list) or not all(isinstance(x, str) for x in refs):
        raise BizError(ErrorCode.PATCH_INVALID, "refs must be a list of strings", {"patch_id": patch_id})
    normalized = dict(row)
    normalized["block_range"] = [start, end]
    normalized["block_start"] = start
    normalized["block_end"] = end
    normalized["block_count"] = count
    normalized.setdefault("operator_name", "metric_op")
    return normalized


def load_patches(data_dir: Path, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks_by_id = {task["case_id"]: task for task in tasks}
    path = None
    for candidate in ("patches.jsonl", "patches.json"):
        if (data_dir / candidate).exists():
            path = data_dir / candidate
            break
    if path is None:
        return []
    rows = read_jsonl_or_array(path)
    seen = set()
    patches = []
    for index, row in enumerate(rows):
        patch = validate_patch(row, tasks_by_id, index)
        if patch["patch_id"] in seen:
            raise BizError(
                ErrorCode.PATCH_INVALID, "duplicate patch_id", {"patch_id": patch["patch_id"]}
            )
        seen.add(patch["patch_id"])
        patches.append(patch)
    return patches


def persist_inputs(
    conn: sqlite3.Connection,
    data_dir: Path,
    clock_ms: int,
    tolerance_ms: int = 5_000,
) -> Dict[str, Any]:
    tasks = load_manifest(data_dir)
    samples = load_and_validate_samples(data_dir, tasks)
    patches = load_patches(data_dir, tasks)
    with immediate(conn):
        advance_clock(conn, clock_ms, tolerance_ms)
        for task in tasks:
            case_digest = canonical_digest(
                {k: v for k, v in task.items() if k not in {"case_digest"}}
            )
            conn.execute(
                "INSERT INTO cases("
                "case_id,data_size,feature_dim,label_dim,dtype,quant_scale,"
                "quant_zero_point,block_plan_json,revision,source_clock_ms,"
                "case_digest,manifest_json,loaded_clock_ms) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(case_id) DO UPDATE SET "
                "data_size=excluded.data_size,feature_dim=excluded.feature_dim,"
                "label_dim=excluded.label_dim,dtype=excluded.dtype,"
                "quant_scale=excluded.quant_scale,quant_zero_point=excluded.quant_zero_point,"
                "block_plan_json=excluded.block_plan_json,revision=excluded.revision,"
                "source_clock_ms=excluded.source_clock_ms,case_digest=excluded.case_digest,"
                "manifest_json=excluded.manifest_json,loaded_clock_ms=excluded.loaded_clock_ms",
                (
                    task["case_id"],
                    task["data_size"],
                    task["feature_dim"],
                    task["label_dim"],
                    task["dtype"],
                    task["quant_scale"],
                    task["quant_zero_point"],
                    json.dumps(task["block_plan"], sort_keys=True, separators=(",", ":")),
                    str(task["revision"]),
                    task["logical_clock_ms"],
                    case_digest,
                    json.dumps(task, sort_keys=True, separators=(",", ":")),
                    clock_ms,
                ),
            )
            for descriptor in samples[task["case_id"]]:
                conn.execute(
                    "INSERT INTO tensor_blocks("
                    "case_id,role,block_index,name,shape_json,descriptor_json,"
                    "content_digest) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(case_id,role,block_index,name) DO UPDATE SET "
                    "shape_json=excluded.shape_json,descriptor_json=excluded.descriptor_json,"
                    "content_digest=excluded.content_digest",
                    (
                        task["case_id"],
                        descriptor["role"],
                        descriptor["block_index"],
                        descriptor["name"],
                        json.dumps(descriptor["shape"], separators=(",", ":")),
                        json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
                        descriptor["digest"],
                    ),
                )
        for patch in patches:
            conn.execute(
                "INSERT INTO patches("
                "patch_id,case_id,block_start,block_end,block_range_json,patch_kind,"
                "patch_generation,logical_clock_ms,new_digest,content_json,refs_json,"
                "received_clock_ms,applied,superseded) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0) "
                "ON CONFLICT(patch_id) DO NOTHING",
                (
                    patch["patch_id"],
                    patch["case_id"],
                    patch["block_start"],
                    patch["block_end"],
                    json.dumps(patch["block_range"]),
                    patch["patch_kind"],
                    patch["patch_generation"],
                    patch["logical_clock_ms"],
                    patch["new_digest"],
                    json.dumps(patch.get("patch_content", {}), sort_keys=True),
                    json.dumps(patch.get("refs", []), sort_keys=True),
                    clock_ms,
                ),
            )
    return {"case_count": len(tasks), "tensor_count": sum(len(v) for v in samples.values()), "patch_count": len(patches)}
