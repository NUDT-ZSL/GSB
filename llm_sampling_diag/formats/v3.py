from __future__ import annotations

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
    ReadResult,
    UnifiedTask,
)
from .common import check_logits_shape, parse_revision_object


def _task_line(lines: list[str], case_id: str, occurrence: int) -> int:
    needle = f'"{case_id}"'
    found = [index + 1 for index, line in enumerate(lines) if needle in line]
    if not found:
        return 1
    return found[min(occurrence, len(found) - 1)]


def _collect_mask(
    selectors: list[Any],
    *,
    field: str,
    line: int,
    case_id: str,
) -> tuple[int, ...]:
    indices: set[int] = set()
    for selector_index, selector_raw in enumerate(selectors):
        selector = require_mapping(
            selector_raw,
            f"{field}[{selector_index}]",
            line,
            case_id,
        )
        op = selector.get("op", "exclude")
        if op != "exclude":
            raise DiagnosticError(
                ErrorCode.E_FORMAT_CORRUPT,
                "v3 candidate mask currently permits only exclude selectors",
                field=f"{field}[{selector_index}].op",
                line=line,
                case_id=case_id,
            )
        if "indices" in selector:
            values = require_list(
                selector["indices"],
                f"{field}[{selector_index}].indices",
                line,
                case_id,
            )
            for index, item in enumerate(values):
                parsed = parse_int(
                    item,
                    f"{field}[{selector_index}].indices[{index}]",
                    line,
                    case_id,
                    minimum=0,
                )
                indices.add(parsed)
        children_key = "selectors" if "selectors" in selector else ("groups" if "groups" in selector else None)
        if children_key is not None:
            children = require_list(
                selector[children_key],
                f"{field}[{selector_index}].{children_key}",
                line,
                case_id,
            )
            nested = _collect_mask(
                children,
                field=f"{field}[{selector_index}].{children_key}",
                line=line,
                case_id=case_id,
            )
            indices.update(nested)
    return tuple(sorted(indices))


def _parse_candidate_mask(
    value: Any,
    *,
    line: int,
    case_id: str,
) -> CandidateMask:
    mask = require_mapping(value, "candidate_mask", line, case_id)
    mask_version = parse_int(
        mask.get("mask_version", 1),
        "candidate_mask.mask_version",
        line,
        case_id,
        minimum=1,
    )
    selectors_raw = require_list(
        mask.get("selectors", []),
        "candidate_mask.selectors",
        line,
        case_id,
    )
    excluded = _collect_mask(selectors_raw, field="candidate_mask.selectors", line=line, case_id=case_id)
    return CandidateMask(
        excluded_indices=excluded,
        selectors=tuple(selectors_raw),
        mask_version=mask_version,
        source="v3_nested",
    )


def _build_task(
    task: dict[str, Any],
    *,
    line: int,
    source_ref: str,
) -> tuple[UnifiedTask, dict[str, Any]]:
    case = require_mapping(require_field(task, "case", "case", line), "case", line)
    case_raw = require_field(case, "id", "case.id", line)
    if not isinstance(case_raw, str) or not case_raw:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "case.id must be a non-empty string",
            field="case.id",
            line=line,
        )
    case_id = case_raw
    effective = require_mapping(
        require_field(case, "effective", "case.effective", line, case_id),
        "case.effective",
        line,
        case_id,
    )
    effective_at_ms = parse_int(
        require_field(effective, "at_unix_ms", "case.effective.at_unix_ms", line, case_id),
        "case.effective.at_unix_ms",
        line,
        case_id,
    )
    logits_object = require_mapping(
        require_field(task, "logits", "logits", line, case_id),
        "logits",
        line,
        case_id,
    )
    dtype = logits_object.get("dtype", "float64")
    if dtype not in {"float32", "float64"}:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "logits.dtype must be float32 or float64",
            field="logits.dtype",
            line=line,
            case_id=case_id,
        )
    logits = parse_logits(
        require_field(logits_object, "values", "logits.values", line, case_id),
        "logits.values",
        line,
        case_id,
    )
    logits_shape = parse_shape(
        require_field(logits_object, "shape", "logits.shape", line, case_id),
        "logits.shape",
        line,
        case_id,
    )
    check_logits_shape(logits_shape, len(logits), field="logits.shape", line=line, case_id=case_id)
    sampling = require_mapping(
        require_field(task, "sampling", "sampling", line, case_id),
        "sampling",
        line,
        case_id,
    )
    k_mode = require_field(sampling, "mode", "sampling.mode", line, case_id)
    if k_mode not in {"top_k", "top_p"}:
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "sampling.mode must be top_k or top_p",
            field="sampling.mode",
            line=line,
            case_id=case_id,
        )
    k_value = parse_float(
        require_field(sampling, "value", "sampling.value", line, case_id),
        "sampling.value",
        line,
        case_id,
    )
    temperature = parse_float(
        require_field(sampling, "temperature", "sampling.temperature", line, case_id),
        "sampling.temperature",
        line,
        case_id,
    )
    if "tie_policy" not in sampling or sampling["tie_policy"] in {None, ""}:
        raise DiagnosticError(
            ErrorCode.E_TIE_POLICY_UNSPECIFIED,
            "sampling.tie_policy is not declared",
            field="sampling.tie_policy",
            line=line,
            case_id=case_id,
        )
    tie_policy = sampling["tie_policy"]
    if not isinstance(tie_policy, str):
        raise DiagnosticError(
            ErrorCode.E_FORMAT_CORRUPT,
            "sampling.tie_policy must be a string",
            field="sampling.tie_policy",
            line=line,
            case_id=case_id,
        )
    if "seed" not in sampling or sampling["seed"] is None:
        raise DiagnosticError(
            ErrorCode.E_SEED_UNSPECIFIED,
            "sampling.seed is not declared",
            field="sampling.seed",
            line=line,
            case_id=case_id,
        )
    seed = parse_int(sampling["seed"], "sampling.seed", line, case_id, minimum=0)
    num_samples = parse_int(
        sampling.get("num_samples", 1),
        "sampling.num_samples",
        line,
        case_id,
        minimum=1,
    )
    top_p = (
        parse_float(sampling["top_p"], "sampling.top_p", line, case_id)
        if sampling.get("top_p") is not None
        else None
    )
    candidate_mask = _parse_candidate_mask(
        require_field(task, "candidate_mask", "candidate_mask", line, case_id),
        line=line,
        case_id=case_id,
    )
    error_budget = parse_float(
        require_field(task, "error_budget", "error_budget", line, case_id),
        "error_budget",
        line,
        case_id,
    )
    revision, revision_id = parse_revision_object(
        require_field(task, "revision", "revision", line, case_id),
        "revision",
        line,
        case_id,
    )
    chunk = require_mapping(
        require_field(task, "chunk_plan", "chunk_plan", line, case_id),
        "chunk_plan",
        line,
        case_id,
    )
    chunk_size = parse_int(
        require_field(chunk, "chunk_size", "chunk_plan.chunk_size", line, case_id),
        "chunk_plan.chunk_size",
        line,
        case_id,
        minimum=1,
    )
    chunk_count = parse_int(
        require_field(chunk, "chunk_count", "chunk_plan.chunk_count", line, case_id),
        "chunk_plan.chunk_count",
        line,
        case_id,
        minimum=1,
    )
    actions = [
        {
            "field": "candidate_mask",
            "action": "preserve_nested",
            "source": "v3_explicit",
            "value": list(candidate_mask.excluded_indices),
        },
        {
            "field": "revision",
            "action": "preserve",
            "source": "v3_explicit",
            "value": revision,
        },
    ]
    unified = UnifiedTask(
        case_id=case_id,
        effective_at_ms=effective_at_ms,
        logits=logits,
        logits_shape=logits_shape,
        k_mode=k_mode,
        k_value=k_value,
        temperature=temperature,
        tie_policy=tie_policy,
        seed=seed,
        candidate_mask=candidate_mask,
        error_budget=error_budget,
        revision=revision,
        revision_id=revision_id,
        chunk_plan=ChunkPlan(chunk_size=chunk_size, chunk_count=chunk_count),
        num_samples=num_samples,
        top_p=top_p,
        source_format="v3",
        source_ref=f"{source_ref}:{line}",
        migration_actions=tuple(actions),
    )
    record = {
        "case_id": case_id,
        "from_format": "v3",
        "to_format": "unified",
        "preserved": actions,
    }
    return unified, record


def read_v3(source: str | Path) -> ReadResult:
    path = Path(source)
    document = load_json_file(path)
    root = document.data
    if not isinstance(root, dict):
        raise DiagnosticError(ErrorCode.E_FORMAT_CORRUPT, "v3 root must be an object", field="$", line=1)
    if "version" in root:
        check_version(document, {3})
    elif root.get("format") != "task-v3":
        raise DiagnosticError(
            ErrorCode.E_VERSION_UNSUPPORTED,
            "cannot dispatch a v3 document without version header or v3 field set",
            field="version",
            line=1,
        )
    tasks_raw = require_list(require_field(root, "tasks", "tasks", 1), "tasks", document.key_line("tasks"))
    tasks: list[UnifiedTask] = []
    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for task_index, task_raw in enumerate(tasks_raw):
        task = require_mapping(task_raw, f"tasks[{task_index}]", document.key_line("tasks"), None)
        provisional_case = None
        if isinstance(task.get("case"), dict):
            provisional_case = task["case"].get("id")
        line = (
            _task_line(document.lines, provisional_case, task_index)
            if isinstance(provisional_case, str)
            else document.key_line("tasks")
        )
        unified, record = _build_task(task, line=line, source_ref=str(path))
        if unified.case_id in seen:
            raise DiagnosticError(
                ErrorCode.E_DUP_CASE,
                f"duplicate case_id {unified.case_id!r}",
                field="case.id",
                line=line,
                case_id=unified.case_id,
                details={"first_line": seen[unified.case_id], "second_line": line},
            )
        seen[unified.case_id] = line
        tasks.append(unified)
        records.append(record)
    return ReadResult(tasks=tasks, records=records)

