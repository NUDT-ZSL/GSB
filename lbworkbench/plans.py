from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import WorkbenchError, bad_plan
from .util import (
    canonical_instance_id,
    canonical_json,
    load_json_file,
    sha256_json,
)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise bad_plan(f"{field} must be an object", field=field)
    return value


def _array(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise bad_plan(f"{field} must be an array", field=field)
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise bad_plan(f"{field} must be a non-empty string", field=field)
    return value


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise bad_plan(f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise bad_plan(f"{field} must be >= {minimum}", field=field, value=value)
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise bad_plan(f"{field} must be a boolean", field=field)
    return value


def _instance_id(value: Any, field: str) -> str:
    try:
        return canonical_instance_id(value, field)
    except WorkbenchError as exc:
        raise bad_plan(exc.message, field=field, value=value) from exc


def parse_plan(path: str | Path) -> dict[str, Any]:
    plan_path = Path(path).expanduser().resolve()
    if not plan_path.is_file():
        raise bad_plan("check plan file does not exist", path=str(plan_path))
    raw = _object(load_json_file(plan_path, error_code="E_BAD_PLAN"), "plan")
    if raw.get("schema") != "lb-check-plan/v1":
        raise bad_plan(
            "plan schema must be lb-check-plan/v1",
            field="schema",
            value=raw.get("schema"),
        )
    plan_id = _string(raw.get("plan_id"), "plan.plan_id")
    interval_ms = _integer(raw.get("interval_ms"), "plan.interval_ms", minimum=1)
    timeout_ms = _integer(raw.get("timeout_ms"), "plan.timeout_ms", minimum=1)
    window_size = _integer(raw.get("window_size"), "plan.window_size", minimum=1)
    failure_threshold = _integer(
        raw.get("failure_threshold"), "plan.failure_threshold", minimum=1
    )
    success_threshold = _integer(
        raw.get("success_threshold"), "plan.success_threshold", minimum=1
    )
    rounds_raw = _array(raw.get("rounds"), "plan.rounds")
    if not rounds_raw:
        raise bad_plan("plan.rounds must not be empty")

    rounds: list[dict[str, Any]] = []
    previous_clock: int | None = None
    seen_round_indexes: set[int] = set()
    for position, raw_round in enumerate(rounds_raw):
        round_obj = _object(raw_round, f"plan.rounds[{position}]")
        round_index = _integer(
            round_obj.get("round_index"),
            f"plan.rounds[{position}].round_index",
            minimum=1,
        )
        if round_index in seen_round_indexes:
            raise bad_plan("duplicate round_index", round_index=round_index)
        seen_round_indexes.add(round_index)
        clock = _integer(
            round_obj.get("logical_clock_ms"),
            f"plan.rounds[{position}].logical_clock_ms",
            minimum=0,
        )
        if previous_clock is not None:
            delta = clock - previous_clock
            if delta != interval_ms:
                raise bad_plan(
                    "round logical clock delta must equal interval_ms",
                    round_index=round_index,
                    expected_delta_ms=interval_ms,
                    actual_delta_ms=delta,
                )
        previous_clock = clock
        probes_raw = _array(
            round_obj.get("probes"), f"plan.rounds[{position}].probes"
        )
        if not probes_raw:
            raise bad_plan("round probes must not be empty", round_index=round_index)

        probes: list[dict[str, Any]] = []
        seen_instances: set[str] = set()
        for probe_position, raw_probe in enumerate(probes_raw):
            probe = _object(
                raw_probe, f"plan.rounds[{position}].probes[{probe_position}]"
            )
            raw_id = _string(
                probe.get("instance_id"),
                f"plan.rounds[{position}].probes[{probe_position}].instance_id",
            )
            instance_id = _instance_id(raw_id, "plan.probe.instance_id")
            if instance_id in seen_instances:
                raise bad_plan(
                    "duplicate probe instance in one round",
                    round_index=round_index,
                    instance_id=instance_id,
                )
            seen_instances.add(instance_id)
            target = _string(
                probe.get("target", probe.get("path")),
                f"plan.rounds[{position}].probes[{probe_position}].target",
            )
            if target.startswith(("http://", "https://")):
                parsed = urlparse(target)
                if not parsed.hostname or parsed.scheme not in {"http", "https"}:
                    raise bad_plan("probe target URL is invalid", target=target)
                if not _is_loopback_host(parsed.hostname):
                    raise bad_plan(
                        "only loopback probe targets are permitted",
                        target=target,
                        hostname=parsed.hostname,
                    )
            elif not target.startswith("/"):
                raise bad_plan(
                    "relative probe target must start with '/'",
                    target=target,
                )
            expected_status = _integer(
                probe.get("expected_status", 200),
                f"plan.rounds[{position}].probes[{probe_position}].expected_status",
                minimum=100,
            )
            if expected_status > 599:
                raise bad_plan("expected_status must be <= 599", value=expected_status)
            require_json = _boolean(
                probe.get("require_json", True),
                f"plan.rounds[{position}].probes[{probe_position}].require_json",
            )
            probe_timeout = _integer(
                probe.get("timeout_ms", timeout_ms),
                f"plan.rounds[{position}].probes[{probe_position}].timeout_ms",
                minimum=1,
            )
            if probe_timeout > timeout_ms:
                raise bad_plan(
                    "probe timeout exceeds plan timeout budget",
                    probe_timeout_ms=probe_timeout,
                    plan_timeout_ms=timeout_ms,
                )
            probes.append(
                {
                    "instance_id": instance_id,
                    "target": target,
                    "expected_status": expected_status,
                    "require_json": require_json,
                    "timeout_ms": probe_timeout,
                }
            )
        probes.sort(key=lambda item: item["instance_id"])
        rounds.append(
            {
                "round_index": round_index,
                "logical_clock_ms": clock,
                "probes": probes,
            }
        )

    rounds.sort(key=lambda item: item["round_index"])
    expected_indexes = list(range(1, len(rounds) + 1))
    actual_indexes = [item["round_index"] for item in rounds]
    if actual_indexes != expected_indexes:
        raise bad_plan(
            "round indexes must be contiguous starting at 1",
            expected=expected_indexes,
            actual=actual_indexes,
        )
    clocks = [item["logical_clock_ms"] for item in rounds]
    if clocks != sorted(clocks):
        raise bad_plan("round logical clocks must increase")
    plan = {
        "schema": "lb-check-plan/v1",
        "plan_id": plan_id,
        "interval_ms": interval_ms,
        "timeout_ms": timeout_ms,
        "window_size": window_size,
        "failure_threshold": failure_threshold,
        "success_threshold": success_threshold,
        "rounds": rounds,
        "plan_digest": sha256_json(
            {
                "plan_id": plan_id,
                "interval_ms": interval_ms,
                "timeout_ms": timeout_ms,
                "window_size": window_size,
                "failure_threshold": failure_threshold,
                "success_threshold": success_threshold,
                "rounds": rounds,
            }
        ),
        "canonical_digest_input": canonical_json(rounds),
        "plan_path": str(plan_path),
    }
    return plan


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
