from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import BusinessError
from .models import (
    DiagnosticConfig,
    ProbeSpec,
    RoundSpec,
    Scenario,
    TargetSpec,
    Tolerance,
)

_TARGET_FIELDS = {
    "name",
    "endpoint",
    "expected_status",
    "period_ms",
    "timeout_ms",
    "failure_threshold",
}
_CONFIG_FIELDS = {
    "max_concurrency",
    "round_budget_ms",
    "window_size",
    "window_step",
    "loss_rate_threshold",
    "latency_jitter_threshold_ms",
    "tolerance",
    "clock_step_ms",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_SUPPORTED_PROBE_TYPES = {"http"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code, message, details)


def _require_object(value: Any, code: str, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code, message)
    return value


def _require_list(value: Any, code: str, message: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(code, message)
    return value


def _require_bool_int(value: Any, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code, f"{field} must be an integer", field=field, value=value)
    return value


def _require_number(value: Any, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code, f"{field} must be a number", field=field, value=value)
    return float(value)


def _require_str(value: Any, code: str, field: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        _fail(code, f"{field} must be a non-empty string", field=field, value=value)
    return value


def _validate_loopback_endpoint(endpoint: str, target_name: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http":
        _fail(
            "TARGET_INVALID_ENDPOINT",
            "only http endpoints are supported",
            target=target_name,
            endpoint=endpoint,
            reason="scheme_must_be_http",
        )
    if parsed.username or parsed.password:
        _fail(
            "TARGET_INVALID_ENDPOINT",
            "endpoint credentials are not allowed",
            target=target_name,
            endpoint=endpoint,
        )
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        _fail(
            "TARGET_NON_LOOPBACK",
            "probe endpoint must use a loopback host",
            target=target_name,
            endpoint=endpoint,
            host=host,
            allowed_hosts=sorted(_LOOPBACK_HOSTS),
        )
    try:
        port = parsed.port
    except ValueError:
        _fail(
            "TARGET_INVALID_ENDPOINT",
            "endpoint port is invalid",
            target=target_name,
            endpoint=endpoint,
        )
    if port is None:
        _fail(
            "TARGET_INVALID_ENDPOINT",
            "endpoint must contain an explicit port",
            target=target_name,
            endpoint=endpoint,
        )
    if parsed.fragment:
        _fail(
            "TARGET_INVALID_ENDPOINT",
            "endpoint fragments are not supported",
            target=target_name,
            endpoint=endpoint,
        )


def _parse_config(raw: Any) -> DiagnosticConfig:
    obj = _require_object(raw, "SCHEMA_INVALID", "config must be an object")
    missing = sorted(_CONFIG_FIELDS - set(obj))
    if missing:
        _fail("SCHEMA_MISSING", "config is missing fields", fields=missing)

    max_concurrency = _require_bool_int(
        obj["max_concurrency"], "CONFIG_CONCURRENCY_RANGE", "max_concurrency"
    )
    if max_concurrency <= 0:
        _fail(
            "CONFIG_CONCURRENCY_RANGE",
            "max_concurrency must be a positive integer",
            value=max_concurrency,
        )
    round_budget = _require_bool_int(
        obj["round_budget_ms"], "CONFIG_BUDGET_RANGE", "round_budget_ms"
    )
    if round_budget <= 0:
        _fail(
            "CONFIG_BUDGET_RANGE",
            "round_budget_ms must be a positive integer",
            value=round_budget,
        )
    window_size = _require_bool_int(
        obj["window_size"], "CONFIG_WINDOW_SIZE_RANGE", "window_size"
    )
    window_step = _require_bool_int(
        obj["window_step"], "CONFIG_WINDOW_STEP_RANGE", "window_step"
    )
    if window_size <= 0:
        _fail(
            "CONFIG_WINDOW_SIZE_RANGE",
            "window_size must be positive",
            value=window_size,
        )
    if window_step <= 0:
        _fail(
            "CONFIG_WINDOW_STEP_RANGE",
            "window_step must be positive",
            value=window_step,
        )
    if window_step > window_size:
        _fail(
            "CONFIG_WINDOW_STEP_RANGE",
            "window_step must not exceed window_size",
            window_size=window_size,
            window_step=window_step,
        )
    loss_threshold = _require_number(
        obj["loss_rate_threshold"],
        "CONFIG_LOSS_THRESHOLD_RANGE",
        "loss_rate_threshold",
    )
    if not 0.0 <= loss_threshold <= 1.0:
        _fail(
            "CONFIG_LOSS_THRESHOLD_RANGE",
            "loss_rate_threshold must be within [0, 1]",
            value=loss_threshold,
        )
    jitter_threshold = _require_number(
        obj["latency_jitter_threshold_ms"],
        "CONFIG_JITTER_THRESHOLD_RANGE",
        "latency_jitter_threshold_ms",
    )
    if jitter_threshold < 0:
        _fail(
            "CONFIG_JITTER_THRESHOLD_RANGE",
            "latency_jitter_threshold_ms must be non-negative",
            value=jitter_threshold,
        )
    tolerance = _require_object(
        obj["tolerance"], "CONFIG_TOLERANCE_INVALID", "tolerance must be an object"
    )
    missing_tol = sorted({"loss_rate", "latency_ms"} - set(tolerance))
    if missing_tol:
        _fail(
            "CONFIG_TOLERANCE_INVALID",
            "tolerance is missing fields",
            fields=missing_tol,
        )
    loss_tol = _require_number(
        tolerance["loss_rate"], "CONFIG_TOLERANCE_INVALID", "tolerance.loss_rate"
    )
    latency_tol = _require_number(
        tolerance["latency_ms"], "CONFIG_TOLERANCE_INVALID", "tolerance.latency_ms"
    )
    if not 0.0 <= loss_tol <= 1.0:
        _fail(
            "CONFIG_TOLERANCE_INVALID",
            "tolerance.loss_rate must be within [0, 1]",
            value=loss_tol,
        )
    if latency_tol < 0:
        _fail(
            "CONFIG_TOLERANCE_INVALID",
            "tolerance.latency_ms must be non-negative",
            value=latency_tol,
        )
    clock_step = _require_bool_int(
        obj["clock_step_ms"], "CONFIG_CLOCK_STEP_RANGE", "clock_step_ms"
    )
    if clock_step <= 0:
        _fail(
            "CONFIG_CLOCK_STEP_RANGE",
            "clock_step_ms must be positive",
            value=clock_step,
        )
    return DiagnosticConfig(
        max_concurrency=max_concurrency,
        round_budget_ms=round_budget,
        window_size=window_size,
        window_step=window_step,
        loss_rate_threshold=loss_threshold,
        latency_jitter_threshold_ms=jitter_threshold,
        tolerance=Tolerance(loss_tol, latency_tol),
        clock_step_ms=clock_step,
    )


def _parse_targets(raw: Any) -> tuple[TargetSpec, ...]:
    items = _require_list(raw, "SCHEMA_INVALID", "targets must be an array")
    if not items:
        _fail("SCHEMA_MISSING", "at least one target is required")
    targets: list[TargetSpec] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        obj = _require_object(
            item, "SCHEMA_INVALID", f"targets[{index}] must be an object"
        )
        missing = sorted(_TARGET_FIELDS - set(obj))
        if missing:
            _fail(
                "SCHEMA_MISSING",
                f"targets[{index}] is missing fields",
                index=index,
                fields=missing,
            )
        name = _require_str(
            obj["name"], "TARGET_NAME_INVALID", f"targets[{index}].name"
        )
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", name):
            _fail(
                "TARGET_NAME_INVALID",
                "target name contains unsupported characters",
                target=name,
            )
        if name in names:
            _fail("TARGET_DUPLICATE_NAME", "target names must be unique", target=name)
        names.add(name)
        endpoint = _require_str(
            obj["endpoint"], "TARGET_INVALID_ENDPOINT", f"targets[{index}].endpoint"
        )
        _validate_loopback_endpoint(endpoint, name)
        expected_status = _require_bool_int(
            obj["expected_status"],
            "TARGET_EXPECTED_STATUS_RANGE",
            f"targets[{index}].expected_status",
        )
        if not 100 <= expected_status <= 599:
            _fail(
                "TARGET_EXPECTED_STATUS_RANGE",
                "expected_status must be within [100, 599]",
                target=name,
                value=expected_status,
            )
        period_ms = _require_bool_int(
            obj["period_ms"], "TARGET_PERIOD_RANGE", f"targets[{index}].period_ms"
        )
        timeout_ms = _require_bool_int(
            obj["timeout_ms"], "TARGET_TIMEOUT_RANGE", f"targets[{index}].timeout_ms"
        )
        threshold = _require_bool_int(
            obj["failure_threshold"],
            "TARGET_FAILURE_THRESHOLD_RANGE",
            f"targets[{index}].failure_threshold",
        )
        if period_ms <= 0:
            _fail(
                "TARGET_PERIOD_RANGE",
                "period_ms must be a positive integer",
                target=name,
                value=period_ms,
            )
        if timeout_ms <= 0:
            _fail(
                "TARGET_TIMEOUT_RANGE",
                "timeout_ms must be a positive integer",
                target=name,
                value=timeout_ms,
            )
        if period_ms < timeout_ms:
            _fail(
                "TARGET_PERIOD_LT_TIMEOUT",
                "period_ms must be greater than or equal to timeout_ms",
                target=name,
                period_ms=period_ms,
                timeout_ms=timeout_ms,
            )
        if threshold <= 0:
            _fail(
                "TARGET_FAILURE_THRESHOLD_RANGE",
                "failure_threshold must be a positive integer",
                target=name,
                value=threshold,
            )
        targets.append(
            TargetSpec(
                name=name,
                endpoint=endpoint,
                expected_status=expected_status,
                period_ms=period_ms,
                timeout_ms=timeout_ms,
                failure_threshold=threshold,
            )
        )
    return tuple(targets)


def _parse_script(
    raw: Any, targets: dict[str, TargetSpec], clock_step: int
) -> tuple[tuple[int, ...], tuple[RoundSpec, ...]]:
    obj = _require_object(
        raw, "SCHEMA_INVALID", "probe_script must be an object"
    )
    missing = sorted({"clock_step_table", "rounds"} - set(obj))
    if missing:
        _fail(
            "SCHEMA_MISSING", "probe_script is missing fields", fields=missing
        )
    table_raw = _require_list(
        obj["clock_step_table"],
        "CLOCK_STEP_TABLE_INVALID",
        "clock_step_table must be an array",
    )
    if not table_raw:
        _fail("CLOCK_STEP_TABLE_INVALID", "clock_step_table must not be empty")
    table: list[int] = []
    for index, value in enumerate(table_raw):
        tick = _require_bool_int(
            value,
            "CLOCK_STEP_TABLE_INVALID",
            f"clock_step_table[{index}]",
        )
        if tick < 0:
            _fail(
                "CLOCK_STEP_TABLE_INVALID",
                "clock steps must be non-negative",
                value=tick,
            )
        if tick % clock_step != 0:
            _fail(
                "CLOCK_STEP_TABLE_INVALID",
                "clock steps must align to config.clock_step_ms",
                value=tick,
                clock_step_ms=clock_step,
            )
        if table and tick <= table[-1]:
            _fail(
                "CLOCK_STEP_TABLE_INVALID",
                "clock steps must be strictly increasing",
                previous=table[-1],
                value=tick,
            )
        table.append(tick)

    rounds_raw = _require_list(
        obj["rounds"], "SCHEMA_INVALID", "probe_script.rounds must be an array"
    )
    if not rounds_raw:
        _fail("SCHEMA_MISSING", "at least one probe round is required")

    rounds: list[RoundSpec] = []
    round_numbers: set[int] = set()
    seqs: set[int] = set()
    plan_index = 0
    previous_tick = -1
    allowed_ticks = set(table)
    for round_index, round_raw in enumerate(rounds_raw):
        round_obj = _require_object(
            round_raw,
            "SCHEMA_INVALID",
            f"probe_script.rounds[{round_index}] must be an object",
        )
        missing_round = sorted({"round", "logical_tick", "probes"} - set(round_obj))
        if missing_round:
            _fail(
                "SCHEMA_MISSING",
                f"rounds[{round_index}] is missing fields",
                fields=missing_round,
            )
        round_no = _require_bool_int(
            round_obj["round"],
            "ROUND_NUMBER_RANGE",
            f"rounds[{round_index}].round",
        )
        logical_tick = _require_bool_int(
            round_obj["logical_tick"],
            "ROUND_CLOCK_INVALID",
            f"rounds[{round_index}].logical_tick",
        )
        if round_no <= 0:
            _fail(
                "ROUND_NUMBER_RANGE",
                "round numbers must be positive",
                value=round_no,
            )
        if round_no in round_numbers:
            _fail(
                "ROUND_DUPLICATE",
                "round numbers must be unique",
                round_no=round_no,
            )
        round_numbers.add(round_no)
        if logical_tick not in allowed_ticks:
            _fail(
                "ROUND_CLOCK_INVALID",
                "round logical_tick must be present in clock_step_table",
                round_no=round_no,
                logical_tick=logical_tick,
            )
        if logical_tick <= previous_tick:
            _fail(
                "ROUND_CLOCK_INVALID",
                "round logical ticks must be strictly increasing",
                round_no=round_no,
                logical_tick=logical_tick,
                previous=previous_tick,
            )
        previous_tick = logical_tick
        probes_raw = _require_list(
            round_obj["probes"],
            "SCHEMA_INVALID",
            f"rounds[{round_index}].probes must be an array",
        )
        if not probes_raw:
            _fail(
                "SCHEMA_MISSING",
                "each round must contain at least one probe",
                round_no=round_no,
            )
        probes: list[ProbeSpec] = []
        for probe_index, probe_raw in enumerate(probes_raw):
            probe_obj = _require_object(
                probe_raw,
                "SCHEMA_INVALID",
                f"rounds[{round_index}].probes[{probe_index}] must be an object",
            )
            missing_probe = sorted({"seq", "target", "type"} - set(probe_obj))
            if missing_probe:
                _fail(
                    "SCHEMA_MISSING",
                    f"probes[{probe_index}] is missing fields",
                    round_no=round_no,
                    fields=missing_probe,
                )
            seq = _require_bool_int(
                probe_obj["seq"],
                "PROBE_SEQ_RANGE",
                f"rounds[{round_index}].probes[{probe_index}].seq",
            )
            if seq <= 0:
                _fail(
                    "PROBE_SEQ_RANGE",
                    "probe seq must be positive",
                    value=seq,
                )
            if seq in seqs:
                _fail(
                    "PROBE_DUPLICATE_SEQ",
                    "probe seq values must be unique",
                    seq=seq,
                    round_no=round_no,
                )
            seqs.add(seq)
            target_name = _require_str(
                probe_obj["target"],
                "PROBE_TARGET_INVALID",
                f"rounds[{round_index}].probes[{probe_index}].target",
            )
            if target_name not in targets:
                _fail(
                    "PROBE_UNKNOWN_TARGET",
                    "probe references an unknown target",
                    target=target_name,
                    seq=seq,
                )
            probe_type = _require_str(
                probe_obj["type"],
                "PROBE_TYPE_INVALID",
                f"rounds[{round_index}].probes[{probe_index}].type",
            )
            if probe_type not in _SUPPORTED_PROBE_TYPES:
                _fail(
                    "PROBE_TYPE_INVALID",
                    "unsupported probe type",
                    value=probe_type,
                    allowed=sorted(_SUPPORTED_PROBE_TYPES),
                )
            probes.append(
                ProbeSpec(
                    seq=seq,
                    round_no=round_no,
                    target=target_name,
                    probe_type=probe_type,
                    logical_tick=logical_tick,
                    plan_index=plan_index,
                )
            )
            plan_index += 1
        rounds.append(
            RoundSpec(round_no=round_no, logical_tick=logical_tick, probes=tuple(probes))
        )
    ticks_by_target: dict[str, list[int]] = {}
    for round_spec in rounds:
        for probe in round_spec.probes:
            ticks_by_target.setdefault(probe.target, []).append(
                round_spec.logical_tick
            )
    for target_name, ticks in ticks_by_target.items():
        unique_ticks = sorted(set(ticks))
        for previous, current in zip(unique_ticks, unique_ticks[1:]):
            if current - previous < targets[target_name].period_ms:
                _fail(
                    "PROBE_PERIOD_VIOLATION",
                    "consecutive probe rounds are closer than the target period",
                    target=target_name,
                    period_ms=targets[target_name].period_ms,
                    previous_tick=previous,
                    current_tick=current,
                )
    return tuple(table), tuple(rounds)


def parse_scenario(raw: Any) -> Scenario:
    obj = _require_object(raw, "SCHEMA_INVALID", "scenario must be an object")
    required = {"schema_version", "scenario_id", "targets", "probe_script", "config"}
    missing = sorted(required - set(obj))
    if missing:
        _fail("SCHEMA_MISSING", "scenario is missing fields", fields=missing)
    schema_version = _require_bool_int(
        obj["schema_version"], "SCHEMA_VERSION_INVALID", "schema_version"
    )
    if schema_version != 1:
        _fail(
            "SCHEMA_VERSION_INVALID",
            "unsupported schema_version",
            value=schema_version,
            supported=[1],
        )
    scenario_id = _require_str(
        obj["scenario_id"], "SCENARIO_ID_INVALID", "scenario_id"
    )
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", scenario_id):
        _fail(
            "SCENARIO_ID_INVALID",
            "scenario_id contains unsupported characters",
            value=scenario_id,
        )
    config = _parse_config(obj["config"])
    targets = _parse_targets(obj["targets"])
    target_map = {target.name: target for target in targets}
    table, rounds = _parse_script(
        obj["probe_script"], target_map, config.clock_step_ms
    )
    return Scenario(
        schema_version=schema_version,
        scenario_id=scenario_id,
        targets=targets,
        rounds=rounds,
        config=config,
        clock_step_table=table,
        raw=obj,
    )


def load_scenario(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            "SCENARIO_READ_FAILED",
            "cannot read scenario file",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "INVALID_JSON",
            "scenario is not valid JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    return parse_scenario(raw)


def scenario_payload(scenario: Scenario) -> dict[str, Any]:
    return {
        "schema_version": scenario.schema_version,
        "scenario_id": scenario.scenario_id,
        "targets": [
            {
                "name": target.name,
                "endpoint": target.endpoint,
                "expected_status": target.expected_status,
                "period_ms": target.period_ms,
                "timeout_ms": target.timeout_ms,
                "failure_threshold": target.failure_threshold,
            }
            for target in scenario.targets
        ],
        "probe_script": {
            "clock_step_table": list(scenario.clock_step_table),
            "rounds": [
                {
                    "round": round_spec.round_no,
                    "logical_tick": round_spec.logical_tick,
                    "probes": [
                        {
                            "seq": probe.seq,
                            "target": probe.target,
                            "type": probe.probe_type,
                        }
                        for probe in round_spec.probes
                    ],
                }
                for round_spec in scenario.rounds
            ],
        },
        "config": {
            "max_concurrency": scenario.config.max_concurrency,
            "round_budget_ms": scenario.config.round_budget_ms,
            "window_size": scenario.config.window_size,
            "window_step": scenario.config.window_step,
            "loss_rate_threshold": scenario.config.loss_rate_threshold,
            "latency_jitter_threshold_ms": scenario.config.latency_jitter_threshold_ms,
            "tolerance": {
                "loss_rate": scenario.config.tolerance.loss_rate,
                "latency_ms": scenario.config.tolerance.latency_ms,
            },
            "clock_step_ms": scenario.config.clock_step_ms,
        },
    }


def scenario_digests(scenario: Scenario) -> tuple[str, str, str]:
    payload = scenario_payload(scenario)
    content_digest = digest_json(payload)
    config_digest = digest_json(payload["config"])
    scenario_digest = digest_json(
        {
            "schema_version": payload["schema_version"],
            "scenario_id": payload["scenario_id"],
            "targets": payload["targets"],
            "probe_script": payload["probe_script"],
        }
    )
    return content_digest, config_digest, scenario_digest
