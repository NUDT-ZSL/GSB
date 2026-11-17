"""Candidate JSONL, parameter package, and injection-list loading."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .errors import (
    E_CANDIDATE_ID_DUPLICATE,
    E_CANDIDATE_ID_EMPTY,
    E_GROUP_UNKNOWN,
    E_JSON_INVALID,
    E_NON_FINITE,
    E_PARAM_INVALID,
    E_REQ_EMPTY,
    E_SHAPE_MISMATCH,
    E_SPARSE_INVALID,
    E_WEIGHT_SHAPE,
    FailureRecord,
    PipelineError,
)
from .io_utils import content_digest, read_json
from .models import Candidate, InjectionSpec, LoadResult, ParamsPackage, RequestData


VALID_INJECTION_STAGES = {"load", "normalize", "quantize", "fusion", "truncate"}
VALID_INJECTION_FAULTS = {
    "non_finite_feature",
    "weight_length_mismatch",
    "width_invalid",
    "unknown_feature_group",
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def load_params(path: str | Path, cfg: PipelineConfig) -> tuple[ParamsPackage | None, FailureRecord | None]:
    params_path = Path(path).resolve()
    try:
        raw = read_json(params_path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type=type(exc).__name__,
            message=f"cannot read parameter package: {exc}",
            injection_ids=[],
        )
    if not isinstance(raw, dict):
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="parameter package root must be an object",
            injection_ids=[],
        )

    weights_raw = raw.get("feature_weights")
    bias_raw = raw.get("bias")
    groups_raw = raw.get("feature_groups")
    if not isinstance(weights_raw, list):
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="feature_weights must be a list",
            injection_ids=[],
        )
    if not _is_number(bias_raw):
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="bias must be a finite number",
            injection_ids=[],
        )
    weights: list[float] = []
    for index, value in enumerate(weights_raw):
        if not _is_number(value) or not math.isfinite(float(value)):
            return None, FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"feature weight {index} is not finite",
                injection_ids=[],
            )
        weights.append(float(value))
    bias = float(bias_raw)
    if not math.isfinite(bias):
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="bias is not finite",
            injection_ids=[],
        )

    if not isinstance(groups_raw, dict) or not groups_raw:
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_GROUP_UNKNOWN,
            exception_type="ValueError",
            message="feature_groups must be a non-empty object",
            injection_ids=[],
        )

    known_groups = set(cfg.known_feature_groups)
    groups: dict[str, tuple[int, ...]] = {}
    seen_indices: dict[int, str] = {}
    for group_name_raw, indices_raw in groups_raw.items():
        group_name = str(group_name_raw)
        if known_groups and group_name not in known_groups:
            return None, FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_GROUP_UNKNOWN,
                exception_type="ValueError",
                message=f"feature group {group_name!r} is not declared in known_feature_groups",
                injection_ids=[],
            )
        if not isinstance(indices_raw, list):
            return None, FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_GROUP_UNKNOWN,
                exception_type="ValueError",
                message=f"feature group {group_name!r} must contain an index list",
                injection_ids=[],
            )
        indices: list[int] = []
        for index_raw in indices_raw:
            if isinstance(index_raw, bool) or not isinstance(index_raw, int):
                return None, FailureRecord(
                    request_id="__global__",
                    stage="load",
                    code=E_GROUP_UNKNOWN,
                    exception_type="ValueError",
                    message=f"feature group {group_name!r} has a non-integer index",
                    injection_ids=[],
                )
            if index_raw < 0 or index_raw >= len(weights):
                return None, FailureRecord(
                    request_id="__global__",
                    stage="load",
                    code=E_GROUP_UNKNOWN,
                    exception_type="ValueError",
                    message=f"feature group {group_name!r} index {index_raw} is outside weight range",
                    injection_ids=[],
                )
            if index_raw in seen_indices:
                return None, FailureRecord(
                    request_id="__global__",
                    stage="load",
                    code=E_GROUP_UNKNOWN,
                    exception_type="ValueError",
                    message=(
                        f"feature index {index_raw} appears in multiple groups "
                        f"({seen_indices[index_raw]!r}, {group_name!r})"
                    ),
                    injection_ids=[],
                )
            seen_indices[index_raw] = group_name
            indices.append(index_raw)
        if not indices:
            return None, FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_GROUP_UNKNOWN,
                exception_type="ValueError",
                message=f"feature group {group_name!r} is empty",
                injection_ids=[],
            )
        groups[group_name] = tuple(indices)

    missing = [index for index in range(len(weights)) if index not in seen_indices]
    if missing:
        return None, FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_GROUP_UNKNOWN,
            exception_type="ValueError",
            message=f"feature indices not assigned to any group: {missing}",
            injection_ids=[],
        )

    feature_names_raw = raw.get("feature_names")
    if feature_names_raw is None:
        feature_names = tuple(f"f{index}" for index in range(len(weights)))
    else:
        if (
            not isinstance(feature_names_raw, list)
            or len(feature_names_raw) != len(weights)
            or not all(isinstance(item, str) and item for item in feature_names_raw)
        ):
            return None, FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message="feature_names must be a string list matching feature_weights",
                injection_ids=[],
            )
        feature_names = tuple(str(item) for item in feature_names_raw)

    ordered_groups = {
        group_name: groups[group_name]
        for group_name in sorted(groups, key=lambda name: (min(groups[name]), name))
    }
    package = ParamsPackage(
        path=str(params_path),
        raw=raw,
        params_hash=content_digest(raw),
        version=str(raw.get("version", "unversioned")),
        feature_weights=tuple(weights),
        bias=bias,
        feature_groups=ordered_groups,
        feature_names=feature_names,
    )
    return package, None


def _load_numeric_vector(value: Any, field: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise PipelineError(E_SHAPE_MISMATCH, "load", f"{field} must be a list")
    result: list[float] = []
    for index, item in enumerate(value):
        if not _is_number(item) or not math.isfinite(float(item)):
            raise PipelineError(E_NON_FINITE, "load", f"{field}[{index}] must be finite")
        result.append(float(item))
    return tuple(result)


def _load_sparse_counts(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise PipelineError(E_SHAPE_MISMATCH, "load", "sparse_counts must be a list")
    result: list[int] = []
    for index, item in enumerate(value):
        if not _is_nonnegative_int(item):
            raise PipelineError(
                E_SPARSE_INVALID,
                "load",
                f"sparse_counts[{index}] must be a non-negative integer",
            )
        result.append(int(item))
    return tuple(result)


def _candidate_from_object(
    obj: dict[str, Any],
    request_id: str,
    source_line: int,
) -> Candidate:
    candidate_id = obj.get("item_id", obj.get("candidate_id"))
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise PipelineError(E_CANDIDATE_ID_EMPTY, "load", "candidate item_id must be non-empty")
    candidate_id = candidate_id.strip()
    recall_scores = _load_numeric_vector(obj.get("recall_scores"), "recall_scores")
    dense_features = _load_numeric_vector(obj.get("dense_features"), "dense_features")
    sparse_counts = _load_sparse_counts(obj.get("sparse_counts", obj.get("sparse_feature_counts", [])))
    if not recall_scores and not dense_features and not sparse_counts:
        raise PipelineError(E_SHAPE_MISMATCH, "load", "candidate feature vector is empty")
    return Candidate(
        request_id=request_id,
        candidate_id=candidate_id,
        recall_scores=recall_scores,
        dense_features=dense_features,
        sparse_counts=sparse_counts,
        source_line=source_line,
    )


def _validate_request(request: RequestData, params: ParamsPackage) -> None:
    if not request.candidates:
        raise PipelineError(E_REQ_EMPTY, "load", "request contains no candidates")
    ids = [candidate.candidate_id for candidate in request.candidates]
    if len(ids) != len(set(ids)):
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        raise PipelineError(
            E_CANDIDATE_ID_DUPLICATE,
            "load",
            f"duplicate candidate IDs in request: {duplicates}",
            candidate_id=duplicates[0] if duplicates else None,
        )
    expected_dim = len(params.feature_weights)
    for candidate in request.candidates:
        actual_dim = (
            len(candidate.recall_scores)
            + len(candidate.dense_features)
            + len(candidate.sparse_counts)
        )
        if actual_dim != expected_dim:
            raise PipelineError(
                E_WEIGHT_SHAPE,
                "load",
                (
                    f"candidate {candidate.candidate_id!r} feature dimension {actual_dim} "
                    f"does not match weight dimension {expected_dim}"
                ),
                candidate_id=candidate.candidate_id,
            )


def load_candidates(
    path: str | Path,
    cfg: PipelineConfig,
    params: ParamsPackage,
) -> tuple[dict[str, RequestData], list[FailureRecord], str]:
    candidates_path = Path(path).resolve()
    raw_lines: list[tuple[int, str]] = []
    try:
        with candidates_path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                raw_lines.append((line_no, line))
    except OSError as exc:
        failure = FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_JSON_INVALID,
            exception_type=type(exc).__name__,
            message=f"cannot read candidate JSONL: {exc}",
            injection_ids=[],
        )
        return {}, [failure], content_digest({"path": str(candidates_path), "error": str(exc)})

    grouped: dict[str, list[Candidate]] = {}
    source_lines: dict[str, list[int]] = {}
    failures: list[FailureRecord] = []
    failed_requests: set[str] = set()

    for line_no, raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            failures.append(
                FailureRecord(
                    request_id=f"<line:{line_no:06d}>",
                    stage="load",
                    code=E_JSON_INVALID,
                    exception_type=type(exc).__name__,
                    message=f"invalid JSON on line {line_no}: {exc.msg}",
                    injection_ids=[],
                    line_no=line_no,
                )
            )
            continue
        if not isinstance(obj, dict):
            failures.append(
                FailureRecord(
                    request_id=f"<line:{line_no:06d}>",
                    stage="load",
                    code=E_JSON_INVALID,
                    exception_type="ValueError",
                    message=f"line {line_no} must contain a JSON object",
                    injection_ids=[],
                    line_no=line_no,
                )
            )
            continue
        request_value = obj.get("request_id")
        request_id = request_value.strip() if isinstance(request_value, str) else ""
        if not request_id:
            failures.append(
                FailureRecord(
                    request_id=f"<line:{line_no:06d}>",
                    stage="load",
                    code=E_REQ_EMPTY,
                    exception_type="ValueError",
                    message=f"request_id is empty on line {line_no}",
                    injection_ids=[],
                    line_no=line_no,
                )
            )
            continue
        if request_id in failed_requests:
            continue

        try:
            if "candidates" in obj:
                candidates_raw = obj.get("candidates")
                if not isinstance(candidates_raw, list):
                    raise PipelineError(E_SHAPE_MISMATCH, "load", "candidates must be a list")
                if not candidates_raw:
                    raise PipelineError(E_REQ_EMPTY, "load", "request contains no candidates")
                parsed = [
                    _candidate_from_object(candidate_raw, request_id, line_no)
                    for candidate_raw in candidates_raw
                    if isinstance(candidate_raw, dict)
                ]
                if len(parsed) != len(candidates_raw):
                    raise PipelineError(E_JSON_INVALID, "load", "candidate entries must be objects")
            else:
                parsed = [_candidate_from_object(obj, request_id, line_no)]
        except PipelineError as exc:
            failed_requests.add(request_id)
            grouped.pop(request_id, None)
            failures.append(
                FailureRecord(
                    request_id=request_id,
                    stage="load",
                    code=exc.code,
                    exception_type=type(exc).__name__,
                    message=exc.message,
                    injection_ids=[],
                    line_no=line_no,
                    candidate_id=exc.candidate_id,
                )
            )
            continue

        grouped.setdefault(request_id, []).extend(parsed)
        source_lines.setdefault(request_id, []).append(line_no)

    requests: dict[str, RequestData] = {}
    for request_id in sorted(grouped):
        if request_id in failed_requests:
            continue
        request = RequestData(
            request_id=request_id,
            candidates=tuple(sorted(grouped[request_id], key=lambda item: item.candidate_id)),
            source_lines=tuple(source_lines.get(request_id, [])),
        )
        try:
            _validate_request(request, params)
        except PipelineError as exc:
            failures.append(
                FailureRecord(
                    request_id=request_id,
                    stage="load",
                    code=exc.code,
                    exception_type=type(exc).__name__,
                    message=exc.message,
                    injection_ids=[],
                    candidate_id=exc.candidate_id,
                )
            )
            continue
        requests[request_id] = request

    failures.sort(key=lambda failure: (failure.request_id, failure.stage, failure.code, failure.line_no or 0))
    candidates_hash = content_digest(
        {
            "lines": [
                {"line_no": line_no, "text": text.rstrip("\r\n")}
                for line_no, text in raw_lines
                if text.strip()
            ]
        }
    )
    return requests, failures, candidates_hash


def load_injections(path: str | Path | None) -> tuple[list[InjectionSpec], FailureRecord | None]:
    if path is None:
        return [], None
    injection_path = Path(path)
    try:
        raw = read_json(injection_path)
    except (OSError, json.JSONDecodeError) as exc:
        return [], FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type=type(exc).__name__,
            message=f"cannot read injection list: {exc}",
            injection_ids=[],
        )
    if not isinstance(raw, dict):
        return [], FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="injection list root must be an object",
            injection_ids=[],
        )
    entries = raw.get("injections", [])
    if not isinstance(entries, list):
        return [], FailureRecord(
            request_id="__global__",
            stage="load",
            code=E_PARAM_INVALID,
            exception_type="ValueError",
            message="injections must be a list",
            injection_ids=[],
        )
    specs: list[InjectionSpec] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection entry {index} must be an object",
                injection_ids=[],
            )
        injection_id = entry.get("id")
        request_id = entry.get("request_id")
        stage = str(entry.get("stage", ""))
        fault_type = str(entry.get("fault_type", ""))
        if not isinstance(injection_id, str) or not injection_id:
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection entry {index} has no non-empty id",
                injection_ids=[],
            )
        if injection_id in seen_ids:
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"duplicate injection id {injection_id!r}",
                injection_ids=[],
            )
        seen_ids.add(injection_id)
        if not isinstance(request_id, str) or not request_id:
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} has no request_id",
                injection_ids=[],
            )
        if stage not in VALID_INJECTION_STAGES:
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} has unknown stage {stage!r}",
                injection_ids=[],
            )
        if fault_type not in VALID_INJECTION_FAULTS:
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} has unknown fault_type {fault_type!r}",
                injection_ids=[],
            )
        candidate_id = entry.get("candidate_id")
        feature_index = entry.get("feature_index")
        width = entry.get("width")
        if candidate_id is not None and not isinstance(candidate_id, str):
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} candidate_id must be a string",
                injection_ids=[],
            )
        if feature_index is not None and (
            isinstance(feature_index, bool) or not isinstance(feature_index, int)
        ):
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} feature_index must be an integer",
                injection_ids=[],
            )
        if width is not None and (isinstance(width, bool) or not isinstance(width, int)):
            return [], FailureRecord(
                request_id="__global__",
                stage="load",
                code=E_PARAM_INVALID,
                exception_type="ValueError",
                message=f"injection {injection_id!r} width must be an integer",
                injection_ids=[],
            )
        specs.append(
            InjectionSpec(
                injection_id=injection_id,
                request_id=request_id,
                stage=stage,
                fault_type=fault_type,
                candidate_id=candidate_id,
                feature_index=feature_index,
                width=width,
            )
        )
    specs.sort(key=lambda item: item.injection_id)
    return specs, None


def load_all(
    cfg: PipelineConfig,
    candidates_path: str | Path,
    params_path: str | Path,
) -> tuple[LoadResult, list[InjectionSpec]]:
    params, params_failure = load_params(params_path, cfg)
    injections, injection_failure = load_injections(cfg.injection_path)
    if params is None:
        return (
            LoadResult(
                requests={},
                failures=[params_failure] if params_failure else [],
                candidates_hash="",
                params=None,
                global_failure=params_failure,
            ),
            injections,
        )
    if injection_failure is not None:
        return (
            LoadResult(
                requests={},
                failures=[injection_failure],
                candidates_hash="",
                params=params,
                global_failure=injection_failure,
            ),
            injections,
        )
    requests, failures, candidates_hash = load_candidates(candidates_path, cfg, params)
    return (
        LoadResult(
            requests=requests,
            failures=failures,
            candidates_hash=candidates_hash,
            params=params,
            global_failure=None,
        ),
        injections,
    )
