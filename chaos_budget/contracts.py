from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ChaosError
from .utils import canonical_json, sha256_json, sha256_text


STATE_PENDING = "pending_admission"
STATE_RUNNING = "running"
STATE_BLOCKED = "blocked_waiting"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_RECOVERED = "recovered"
STATE_ABANDONED = "abandoned"

ACTIVE_STATES = (STATE_RUNNING, STATE_BLOCKED)
TERMINAL_STATES = (STATE_COMPLETED, STATE_FAILED, STATE_ABANDONED)

OBSERVATION_TYPES = {
    "blocked": "blocked",
    "stuck": "blocked",
    "block": "blocked",
    "卡点": "blocked",
    "卡点等待": "blocked",
    "waiting": "blocked",
    "completed": "completed",
    "complete": "completed",
    "finish": "completed",
    "finished": "completed",
    "完成": "completed",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "失败": "failed",
    "recovered": "recovered",
    "recover": "recovered",
    "recovery": "recovered",
    "恢复": "recovered",
}


def _issue(issues: List[Dict[str, Any]], code: str, path: str, message: str) -> None:
    issues.append({"code": code, "path": path, "message": message})


def is_real_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def pick(field: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in field and field[name] is not None:
            return field[name]
    return default


def require_int(
    field: Mapping[str, Any],
    names: Tuple[str, ...],
    path: str,
    issues: List[Dict[str, Any]],
    *,
    minimum: Optional[int] = None,
    positive: bool = False,
    non_negative: bool = False,
    default: Optional[int] = None,
) -> Optional[int]:
    key_path = f"{path}.{names[0]}"
    value = pick(field, *names, default=default)
    if value is None:
        _issue(issues, "MISSING_FIELD", key_path, f"missing required integer field: {names[0]}")
        return None
    if not is_real_int(value):
        _issue(issues, "INVALID_INTEGER", key_path, f"field must be an integer, got {type(value).__name__}")
        return None
    if positive and value <= 0:
        _issue(issues, "INVALID_POSITIVE_INTEGER", key_path, "field must be a positive integer")
    if non_negative and value < 0:
        _issue(issues, "INVALID_NON_NEGATIVE_INTEGER", key_path, "field must be a non-negative integer")
    if minimum is not None and value < minimum:
        _issue(issues, "VALUE_OUT_OF_RANGE", key_path, f"field must be >= {minimum}")
    return value


def require_nonempty_string(
    field: Mapping[str, Any],
    names: Tuple[str, ...],
    path: str,
    issues: List[Dict[str, Any]],
) -> Optional[str]:
    value = pick(field, *names)
    key_path = f"{path}.{names[0]}"
    if not isinstance(value, str) or not value.strip():
        _issue(issues, "INVALID_STRING", key_path, "field must be a non-empty string")
        return None
    return value


def read_json_file(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ChaosError(
            "INPUT_NOT_FOUND",
            f"required input file not found: {path}",
            details={"path": str(path)},
        ) from exc
    except OSError as exc:
        raise ChaosError("INPUT_READ_ERROR", f"cannot read input file: {path}", {"path": str(path)}) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ChaosError(
            "MALFORMED_JSON",
            f"invalid JSON in {path}: {exc.msg}",
            details={"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def extract_list(payload: Any, container_key: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get(container_key), list):
        return payload[container_key]
    raise ChaosError(
        "SCHEMA_VIOLATION",
        f"expected a list or an object with {container_key!r}",
        details={"container_key": container_key},
    )


def normalize_observation_type(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return OBSERVATION_TYPES.get(value.strip()) or OBSERVATION_TYPES.get(value)


def observation_declaration(event: Mapping[str, Any]) -> Dict[str, Any]:
    detail = pick(event, "detail", "declaration", "declared_content", "attributes", default={})
    if detail is None:
        detail = {}
    return {
        "experiment_id": pick(event, "experiment_id", "id"),
        "sequence": pick(event, "sequence", "event_sequence", "seq"),
        "observation_type": normalize_observation_type(pick(event, "observation_type", "type")),
        "clock_ms": pick(
            event,
            "observation_logical_clock_ms",
            "logical_clock_ms",
            "clock_ms",
            "observed_at_clock_ms",
        ),
        "generation": pick(event, "generation", "expected_generation"),
        "target_service": pick(event, "target_service", "service"),
        "detail": detail,
    }


def build_summary(event: Mapping[str, Any]) -> str:
    declaration = observation_declaration(event)
    detail = declaration["detail"]
    detail_text = canonical_json(detail) if detail else "{}"
    return (
        f"{declaration['experiment_id']}#{declaration['sequence']}:"
        f"{declaration['observation_type']}@logical_ms={declaration['clock_ms']}:"
        f"{detail_text}"
    )


def validate_experiments(payload: Any) -> List[Dict[str, Any]]:
    rows = extract_list(payload, "experiments")
    issues: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}

    for index, raw in enumerate(rows):
        path = f"experiments[{index}]"
        if not isinstance(raw, dict):
            _issue(issues, "INVALID_OBJECT", path, "experiment must be a JSON object")
            continue

        experiment_id = require_nonempty_string(
            raw, ("experiment_id", "id"), path, issues
        )
        if experiment_id is not None:
            if experiment_id in seen:
                _issue(
                    issues,
                    "DUPLICATE_EXPERIMENT_ID",
                    f"{path}.experiment_id",
                    f"experiment_id {experiment_id!r} is duplicated at index {seen[experiment_id]}",
                )
            else:
                seen[experiment_id] = index

        target_service = require_nonempty_string(raw, ("target_service", "service"), path, issues)
        fault_type = require_nonempty_string(raw, ("fault_type", "type"), path, issues)
        start_clock = require_int(
            raw,
            ("start_logical_clock_ms", "start_clock_ms", "launch_logical_clock_ms", "launch_clock_ms"),
            path,
            issues,
            non_negative=True,
        )
        duration = require_int(
            raw,
            ("expected_duration_ms", "duration_ms", "expected_duration"),
            path,
            issues,
            positive=True,
        )
        priority = require_int(raw, ("priority", "priority_rank"), path, issues, default=100)

        fault_params = pick(raw, "fault_params", "parameters", "params", default={})
        if not isinstance(fault_params, dict):
            _issue(issues, "INVALID_OBJECT", f"{path}.fault_params", "fault_params must be an object")
            fault_params = {}

        round_id = pick(raw, "round_id", "scheduling_round", "round", default="default")
        if not isinstance(round_id, str) or not round_id.strip():
            _issue(issues, "INVALID_STRING", f"{path}.round_id", "round_id must be a non-empty string")
            round_id = "default"

        if not issues or all(not issue["path"].startswith(path) for issue in issues):
            normalized.append(
                {
                    "experiment_id": experiment_id,
                    "target_service": target_service,
                    "fault_type": fault_type,
                    "fault_params": fault_params,
                    "start_clock_ms": start_clock,
                    "expected_duration_ms": duration,
                    "priority": priority,
                    "round_id": round_id,
                    "raw": raw,
                }
            )

    if issues:
        raise ChaosError(
            "VALIDATION_FAILED",
            f"experiment plan validation failed with {len(issues)} issue(s)",
            details={"issues": issues},
        )
    return normalized


def validate_budget_config(payload: Any) -> Dict[str, Any]:
    raw = payload.get("budget_config", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, dict):
        raise ChaosError("SCHEMA_VIOLATION", "budget config must be a JSON object")

    issues: List[Dict[str, Any]] = []
    per_experiment = require_int(
        raw,
        ("per_experiment_budget", "per_experiment_budget_points", "experiment_budget"),
        "budget_config",
        issues,
        positive=True,
    )
    round_budget = require_int(
        raw,
        ("round_logical_time_budget_ms", "round_time_budget_ms", "round_budget_ms"),
        "budget_config",
        issues,
        positive=True,
    )
    concurrency = require_int(
        raw,
        ("global_concurrency_limit", "concurrency_limit", "max_concurrent_experiments"),
        "budget_config",
        issues,
        positive=True,
    )
    backoff_base = require_int(
        raw, ("backoff_base_ms", "backoff_base"), "budget_config", issues, positive=True
    )
    backoff_cap = require_int(
        raw, ("backoff_cap_ms", "backoff_cap", "max_backoff_ms"), "budget_config", issues, positive=True
    )

    phase_charge = require_int(
        raw, ("phase_charge_points", "phase_cost"), "budget_config", issues, non_negative=True, default=3
    )
    admission_charge = require_int(
        raw, ("admission_charge_points",), "budget_config", issues, non_negative=True, default=0
    )
    cost_per_ms = require_int(raw, ("cost_per_ms",), "budget_config", issues, positive=True, default=1)
    lock_timeout = require_int(raw, ("lock_timeout_seconds",), "budget_config", issues, positive=True, default=10)

    if backoff_base is not None and backoff_cap is not None and backoff_cap < backoff_base:
        _issue(
            issues,
            "INVALID_BACKOFF_RANGE",
            "budget_config.backoff_cap_ms",
            "backoff_cap_ms must be greater than or equal to backoff_base_ms",
        )

    if issues:
        raise ChaosError(
            "VALIDATION_FAILED",
            f"budget config validation failed with {len(issues)} issue(s)",
            details={"issues": issues},
        )

    return {
        "per_experiment_budget": per_experiment,
        "round_logical_time_budget_ms": round_budget,
        "global_concurrency_limit": concurrency,
        "backoff_base_ms": backoff_base,
        "backoff_cap_ms": backoff_cap,
        "phase_charge_points": phase_charge,
        "admission_charge_points": admission_charge,
        "cost_per_ms": cost_per_ms,
        "lock_timeout_seconds": lock_timeout,
        "raw": raw,
    }


def validate_observations(payload: Any, known_experiment_ids: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    rows = extract_list(payload, "observations")
    known = set(known_experiment_ids or [])
    issues: List[Dict[str, Any]] = []
    last_sequence: Dict[str, int] = {}
    normalized: List[Dict[str, Any]] = []

    for index, raw in enumerate(rows):
        path = f"observations[{index}]"
        if not isinstance(raw, dict):
            _issue(issues, "INVALID_OBJECT", path, "observation must be an object")
            continue

        experiment_id = require_nonempty_string(raw, ("experiment_id", "id"), path, issues)
        sequence = require_int(
            raw, ("sequence", "event_sequence", "seq"), path, issues, non_negative=True
        )
        clock_ms = require_int(
            raw,
            (
                "observation_logical_clock_ms",
                "logical_clock_ms",
                "clock_ms",
                "observed_at_clock_ms",
            ),
            path,
            issues,
            non_negative=True,
        )
        observation_type = normalize_observation_type(pick(raw, "observation_type", "type"))
        if observation_type is None:
            _issue(issues, "INVALID_OBSERVATION_TYPE", f"{path}.observation_type", "unsupported observation type")

        summary = pick(raw, "summary", "observation_summary")
        if not isinstance(summary, str) or not summary:
            _issue(issues, "INVALID_SUMMARY", f"{path}.summary", "summary must be a non-empty string")

        if experiment_id is not None and sequence is not None:
            previous = last_sequence.get(experiment_id)
            if previous is not None and sequence <= previous:
                _issue(
                    issues,
                    "EVENT_SEQUENCE_NOT_STRICTLY_INCREASING",
                    f"{path}.sequence",
                    f"sequence {sequence} for {experiment_id!r} is not greater than {previous}",
                )
            last_sequence[experiment_id] = sequence

            if known and experiment_id not in known:
                _issue(
                    issues,
                    "UNKNOWN_EXPERIMENT",
                    f"{path}.experiment_id",
                    f"observation references unknown experiment {experiment_id!r}",
                )

        expected_summary = build_summary(raw) if observation_type is not None else None
        if isinstance(summary, str) and expected_summary is not None and summary != expected_summary:
            _issue(
                issues,
                "SUMMARY_MISMATCH",
                f"{path}.summary",
                "summary is inconsistent with declared observation content",
            )

        digest = pick(raw, "summary_sha256", "content_sha256")
        if digest is not None:
            declaration = observation_declaration(raw)
            expected_digest = sha256_json(declaration)
            if digest != expected_digest:
                _issue(
                    issues,
                    "SUMMARY_DIGEST_MISMATCH",
                    f"{path}.summary_sha256",
                    "summary digest does not match canonical declaration",
                )

        if not any(issue["path"].startswith(path) for issue in issues):
            normalized.append(
                {
                    "experiment_id": experiment_id,
                    "sequence": sequence,
                    "observation_type": observation_type,
                    "clock_ms": clock_ms,
                    "summary": summary,
                    "detail": pick(raw, "detail", "declaration", "declared_content", default={}),
                    "generation": pick(raw, "generation", "expected_generation"),
                    "event_id": pick(raw, "event_id"),
                    "raw": raw,
                }
            )

    if issues:
        raise ChaosError(
            "VALIDATION_FAILED",
            f"observation validation failed with {len(issues)} issue(s)",
            details={"issues": issues},
        )
    normalized.sort(key=lambda event: (event["experiment_id"], event["sequence"]))
    return normalized


def input_file_hash(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_fingerprint(file_hashes: Mapping[str, str]) -> str:
    return sha256_json(dict(sorted(file_hashes.items())))


def event_request_hash(event: Mapping[str, Any]) -> str:
    declaration = observation_declaration(event)
    return sha256_json(declaration)


def safe_int(value: Any, default: int = 0) -> int:
    return value if is_real_int(value) else default
