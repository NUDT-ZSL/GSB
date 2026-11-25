from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import DiagnosticError, ErrorCode
from ..jsonutil import (
    check_version,
    load_json_file,
    parse_float,
    parse_int,
    parse_logits,
    parse_shape,
    require_field,
    require_list,
    require_mapping,
)
from ..model import (
    CandidateMask,
    ChunkPlan,
    DEFAULT_ERROR_BUDGET,
    DEFAULT_REVISION,
    DEFAULT_REVISION_ID,
    ReadResult,
    UnifiedTask,
)
from .common import check_logits_shape, parse_flat_mask


def _looks_like_v1(root: dict[str, Any]) -> bool:
    if root.get("format") in {"task-v1", "task-v1-jsonl"}:
        return True
    tasks = root.get("tasks")
    return isinstance(tasks, list) and bool(tasks) and isinstance(tasks[0], dict) and "effective_after_s" in tasks[0]


def _build_task(
    task: dict[str, Any],
    *,
    base_time_ms: int,
    line: int,
    source_ref: str,
    default_chunk_size: int,
) -> tuple[UnifiedTask, dict[str, Any]]:
    case_value = require_field(task, "case_id", "case_id", line)
    if not isinstance(case_value, str) or not case_value:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "case_id must be a non-empty string",
            field="case_id",
            line=line,
        )
    case_id = case_value
    after_value = require_field(task, "effective_after_s", "effective_after_s", line, case_id)
    after_seconds = parse_float(after_value, "effective_after_s", line, case_id)
    if after_seconds < 0:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "effective_after_s must be non-negative",
            field="effective_after_s",
            line=line,
            case_id=case_id,
        )
    effective_at_ms = int(base_time_ms + round(after_seconds * 1000.0))
    logits = parse_logits(require_field(task, "logits", "logits", line, case_id), "logits", line, case_id)
    declared_shape = (
        parse_shape(task["logits_shape"], "logits_shape", line, case_id)
        if "logits_shape" in task
        else (len(logits),)
    )
    check_logits_shape(declared_shape, len(logits), field="logits_shape", line=line, case_id=case_id)
    k_mode = require_field(task, "k_mode", "k_mode", line, case_id)
    if not isinstance(k_mode, str):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "k_mode must be a string",
            field="k_mode",
            line=line,
            case_id=case_id,
        )
    k_value = parse_float(require_field(task, "k_value", "k_value", line, case_id), "k_value", line, case_id)
    temperature = parse_float(
        require_field(task, "temperature", "temperature", line, case_id),
        "temperature",
        line,
        case_id,
    )
    if "tie_policy" not in task or task["tie_policy"] in {None, ""}:
        raise DiagnosticError(
            ErrorCode.E_TIE_POLICY_UNSPECIFIED,
            "tie_policy is not declared",
            field="tie_policy",
            line=line,
            case_id=case_id,
        )
    tie_policy = task["tie_policy"]
    if not isinstance(tie_policy, str):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "tie_policy must be a string",
            field="tie_policy",
            line=line,
            case_id=case_id,
        )
    if "seed" not in task or task["seed"] is None:
        raise DiagnosticError(
            ErrorCode.E_SEED_UNSPECIFIED,
            "seed is not declared",
            field="seed",
            line=line,
            case_id=case_id,
        )
    seed = parse_int(task["seed"], "seed", line, case_id, minimum=0)
    mask_value = task.get("mask_excluded_indices", task.get("candidate_mask_flat", []))
    mask = parse_flat_mask(mask_value, field="mask_excluded_indices", line=line, case_id=case_id)
    error_budget = (
        parse_float(task["error_budget"], "error_budget", line, case_id)
        if "error_budget" in task
        else DEFAULT_ERROR_BUDGET
    )
    num_samples = (
        parse_int(task["num_samples"], "num_samples", line, case_id, minimum=1)
        if "num_samples" in task
        else 1
    )
    top_p = parse_float(task["top_p"], "top_p", line, case_id) if task.get("top_p") is not None else None
    chunk_size = (
        parse_int(task["chunk_size"], "chunk_size", line, case_id, minimum=1)
        if "chunk_size" in task
        else default_chunk_size
    )
    chunk_plan = ChunkPlan.for_logits(len(logits), chunk_size)
    actions: list[dict[str, Any]] = [
        {
            "field": "candidate_mask",
            "action": "default_empty",
            "source": "v1_missing_mask_field" if "mask_excluded_indices" not in task and "candidate_mask_flat" not in task else "v1_flat_mask",
            "value": list(mask),
        },
        {
            "field": "error_budget",
            "action": "default",
            "source": "v1_missing_error_budget" if "error_budget" not in task else "v1_explicit",
            "value": error_budget,
        },
        {
            "field": "revision",
            "action": "default",
            "source": "v1_has_no_revision",
            "value": DEFAULT_REVISION,
        },
        {
            "field": "chunk_plan",
            "action": "default",
            "source": "v1_has_no_chunk_plan" if not task.get("chunk_size") else "v1_explicit",
            "value": chunk_plan.to_dict(),
        },
        {
            "field": "num_samples",
            "action": "default",
            "source": "v1_missing_num_samples" if "num_samples" not in task else "v1_explicit",
            "value": num_samples,
        },
    ]
    unified = UnifiedTask(
        case_id=case_id,
        effective_at_ms=effective_at_ms,
        logits=logits,
        logits_shape=declared_shape,
        k_mode=k_mode,
        k_value=k_value,
        temperature=temperature,
        tie_policy=tie_policy,
        seed=seed,
        candidate_mask=CandidateMask(
            excluded_indices=mask,
            selectors=(),
            mask_version=1,
            source="v1_flat",
        ),
        error_budget=error_budget,
        revision=DEFAULT_REVISION,
        revision_id=DEFAULT_REVISION_ID,
        chunk_plan=chunk_plan,
        num_samples=num_samples,
        top_p=top_p,
        source_format="v1",
        source_ref=f"{source_ref}:{line}",
        migration_actions=tuple(actions),
    )
    record = {
        "case_id": case_id,
        "from_format": "v1",
        "to_format": "unified",
        "upgrade_defaults": actions,
    }
    return unified, record


def _task_line(lines: list[str], case_id: str, occurrence: int) -> int:
    needle = f'"{case_id}"'
    found = [index + 1 for index, line in enumerate(lines) if needle in line]
    if not found:
        return 1
    return found[min(occurrence, len(found) - 1)]


def read_v1(
    source: str | Path,
    *,
    base_time_ms: int | None = None,
    default_chunk_size: int = 4,
) -> ReadResult:
    path = Path(source)
    if path.suffix.lower() == ".jsonl":
        return _read_v1_jsonl(path, base_time_ms=base_time_ms, default_chunk_size=default_chunk_size)
    document = load_json_file(path)
    root = document.data
    if not isinstance(root, dict):
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "v1 root must be an object", field="$", line=1)
    if "version" in root:
        check_version(document, {1})
    elif not _looks_like_v1(root):
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            "cannot dispatch a v1 document without version header or v1 field set",
            field="version",
            line=1,
        )
    root_base = root.get("base_time_unix_ms")
    if base_time_ms is None:
        if root_base is None:
            raise DiagnosticError(
                ErrorCode.E_FORMAT_CORRUPT,
                "missing v1 base_time_unix_ms anchor",
                field="base_time_unix_ms",
                line=document.key_line("base_time_unix_ms"),
            )
        base_time_ms = parse_int(root_base, "base_time_unix_ms", document.key_line("base_time_unix_ms"))
    if not isinstance(base_time_ms, int) or isinstance(base_time_ms, bool):
        raise DiagnosticError(
            ErrorCode.E_CONFIG_INVALID,
            "base_time_ms must be an integer",
            field="base_time_ms",
        )
    tasks_value = require_field(root, "tasks", "tasks", 1)
    tasks_raw = require_list(tasks_value, "tasks", document.key_line("tasks"))
    tasks: list[UnifiedTask] = []
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for task_index, task_raw in enumerate(tasks_raw):
        task = require_mapping(task_raw, f"tasks[{task_index}]", document.key_line("tasks"), None)
        provisional_case = task.get("case_id")
        line = (
            _task_line(document.lines, provisional_case, task_index)
            if isinstance(provisional_case, str)
            else document.key_line("tasks")
        )
        unified, record = _build_task(
            task,
            base_time_ms=base_time_ms,
            line=line,
            source_ref=str(path),
            default_chunk_size=default_chunk_size,
        )
        if unified.case_id in seen:
            raise DiagnosticError(
                ErrorCode.E_DUP_CASE,
                f"duplicate case_id {unified.case_id!r}",
                field="case_id",
                line=line,
                case_id=unified.case_id,
                details={"first_line": seen[unified.case_id], "second_line": line},
            )
        seen[unified.case_id] = line
        tasks.append(unified)
        records.append(record)
    return ReadResult(tasks=tasks, records=records)


def _read_v1_jsonl(
    path: Path,
    *,
    base_time_ms: int | None,
    default_chunk_size: int,
) -> ReadResult:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DiagnosticError(ErrorCode.E_IO, f"cannot read input file: {exc}", details={"path": str(path)}) from exc
    nonempty = [(index + 1, line) for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "v1 JSONL file is empty", field="$", line=1)
    header_line, header_text = nonempty[0]
    try:
        header = json.loads(header_text)
    except json.JSONDecodeError as exc:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            f"invalid JSONL header: {exc.msg}",
            field="<json>",
            line=header_line,
        ) from exc
    if not isinstance(header, dict):
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "JSONL header must be an object", field="$", line=header_line)
    version = header.get("version")
    if version not in {1, "1", "v1"}:
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            f"unsupported JSONL version {version!r}",
            field="version",
            line=header_line,
        )
    if base_time_ms is None:
        base_time_ms = parse_int(header.get("base_time_unix_ms"), "base_time_unix_ms", header_line, minimum=0)
    tasks: list[UnifiedTask] = []
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for line_number, text in nonempty[1:]:
        try:
            task = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DiagnosticError(
                ErrorCode.E_FORMAT_CORRUPT,
                f"invalid task JSON: {exc.msg}",
                field="<json>",
                line=line_number,
            ) from exc
        if not isinstance(task, dict):
            raise DiagnosticError(
                ErrorCode.E_FORMAT_CORRUPT,
                "JSONL task record must be an object",
                field="$",
                line=line_number,
            )
        unified, record = _build_task(
            task,
            base_time_ms=base_time_ms,
            line=line_number,
            source_ref=str(path),
            default_chunk_size=default_chunk_size,
        )
        if unified.case_id in seen:
            raise DiagnosticError(
                ErrorCode.E_DUP_CASE,
                f"duplicate case_id {unified.case_id!r}",
                field="case_id",
                line=line_number,
                case_id=unified.case_id,
                details={"first_line": seen[unified.case_id], "second_line": line_number},
            )
        seen[unified.case_id] = line_number
        tasks.append(unified)
        records.append(record)
    return ReadResult(tasks=tasks, records=records)

