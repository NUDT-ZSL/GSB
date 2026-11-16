"""Historical replay verification against persisted session snapshots."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import EvaluationConfig
from .engine import evaluate_file
from .errors import DomainError, ReplayError
from .models import EvaluationSession
from .snapshot import load_snapshot


@dataclass(frozen=True, slots=True)
class ReplayReport:
    verified: bool
    steps_checked: int
    windows_checked: int
    data_version_match: bool
    data_sha256_match: bool
    parameter_match: bool
    adaptive_path: str
    elapsed_ms: int
    elapsed_us: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": "OK",
            "verified": self.verified,
            "steps_checked": self.steps_checked,
            "windows_checked": self.windows_checked,
            "first_mismatch_step": None,
            "first_mismatch_field": None,
            "data_version_match": self.data_version_match,
            "data_sha256_match": self.data_sha256_match,
            "parameter_match": self.parameter_match,
            "adaptive_path": self.adaptive_path,
            "elapsed_ms": self.elapsed_ms,
            "elapsed_us": self.elapsed_us,
        }


def _first_difference(expected: Any, actual: Any, prefix: str = "") -> str | None:
    if type(expected) is not type(actual):
        return prefix or "value"
    if isinstance(expected, dict):
        expected_keys = sorted(expected)
        actual_keys = sorted(actual)
        if expected_keys != actual_keys:
            missing = sorted(set(expected_keys) ^ set(actual_keys))
            return f"{prefix}.{missing[0]}" if prefix else missing[0]
        for key in expected_keys:
            field = f"{prefix}.{key}" if prefix else key
            difference = _first_difference(expected[key], actual[key], field)
            if difference is not None:
                return difference
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{prefix}.length" if prefix else "length"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual, strict=True)):
            difference = _first_difference(expected_item, actual_item, f"{prefix}[{index}]")
            if difference is not None:
                return difference
        return None
    return None if expected == actual else (prefix or "value")


def _config_difference(expected: EvaluationConfig, snapshot: EvaluationConfig) -> str | None:
    return _first_difference(snapshot.to_dict(), expected.to_dict(), "config")


def _raise_mismatch(step: int | None, field: str, message: str, details: dict[str, Any] | None = None) -> None:
    raise ReplayError(
        message,
        first_mismatch_step=step,
        field=field,
        details=details,
    )


def _compare_session(expected: EvaluationSession, actual: EvaluationSession) -> None:
    if len(expected.long_windows) != len(actual.long_windows):
        _raise_mismatch(
            min(len(expected.long_windows), len(actual.long_windows)),
            "windows.long.length",
            "replayed long-window count differs from snapshot",
            {
                "snapshot": len(expected.long_windows),
                "recomputed": len(actual.long_windows),
            },
        )
    if len(expected.short_windows) != len(actual.short_windows):
        _raise_mismatch(
            min(len(expected.short_windows), len(actual.short_windows)),
            "windows.short.length",
            "replayed short-window count differs from snapshot",
            {
                "snapshot": len(expected.short_windows),
                "recomputed": len(actual.short_windows),
            },
        )
    if len(expected.steps) != len(actual.steps):
        _raise_mismatch(
            min(len(expected.steps), len(actual.steps)),
            "steps.length",
            "replayed evaluation step count differs from snapshot",
            {"snapshot": len(expected.steps), "recomputed": len(actual.steps)},
        )

    for index in range(len(expected.long_windows)):
        for label, expected_window, actual_window in (
            ("windows.long", expected.long_windows[index], actual.long_windows[index]),
            ("windows.short", expected.short_windows[index], actual.short_windows[index]),
        ):
            difference = _first_difference(
                expected_window.to_dict(), actual_window.to_dict(), f"{label}[{index}]"
            )
            if difference is not None:
                _raise_mismatch(
                    index,
                    difference,
                    "replayed window result differs from snapshot",
                )

    for index in range(len(expected.steps)):
        difference = _first_difference(
            expected.steps[index].to_dict(), actual.steps[index].to_dict(), f"steps[{index}]"
        )
        if difference is not None:
            _raise_mismatch(index, difference, "replayed alert state differs from snapshot")

    if len(expected.alert_records) != len(actual.alert_records):
        _raise_mismatch(
            min(len(expected.alert_records), len(actual.alert_records)),
            "alerts.history.length",
            "replayed alert history length differs from snapshot",
        )
    for index in range(len(expected.alert_records)):
        difference = _first_difference(
            expected.alert_records[index].to_dict(),
            actual.alert_records[index].to_dict(),
            f"alerts.history[{index}]",
        )
        if difference is not None:
            _raise_mismatch(index, difference, "replayed alert history differs from snapshot")

    if len(expected.alert_transitions) != len(actual.alert_transitions):
        first_missing = min(len(expected.alert_transitions), len(actual.alert_transitions))
        if first_missing < len(expected.alert_transitions):
            step = expected.alert_transitions[first_missing].step_index
        elif expected.alert_transitions:
            step = expected.alert_transitions[-1].step_index
        else:
            step = actual.alert_transitions[first_missing].step_index
        _raise_mismatch(
            step,
            "alerts.transitions.length",
            "replayed alert transition count differs from snapshot",
        )
    for index, (expected_transition, actual_transition) in enumerate(
        zip(expected.alert_transitions, actual.alert_transitions, strict=True)
    ):
        difference = _first_difference(
            expected_transition.to_dict(),
            actual_transition.to_dict(),
            f"alerts.transitions[{index}]",
        )
        if difference is not None:
            _raise_mismatch(
                expected_transition.step_index,
                difference,
                "replayed alert transition differs from snapshot",
            )
    if expected.final_alert_state != actual.final_alert_state:
        _raise_mismatch(len(expected.steps) - 1, "alerts.final_state", "final alert state differs")


def replay_snapshot(
    path: str | Path,
    *,
    data_path: str | Path | None = None,
    data_version: str | None = None,
    data_sha256: str | None = None,
    expected_config: EvaluationConfig | None = None,
) -> ReplayReport:
    """Recompute every snapshot step and compare all fixed-point outputs."""

    started_ns = time.perf_counter_ns()
    snapshot = load_snapshot(path)
    session = snapshot.session

    first_step = 0 if session.steps else None
    if expected_config is not None:
        difference = _config_difference(expected_config, session.config)
        if difference is not None:
            _raise_mismatch(
                first_step,
                difference,
                "current window/SLO parameters differ from snapshot; replay is not reproducible",
            )
    if data_version is not None and data_version != session.data_version:
        _raise_mismatch(
            first_step,
            "data_version",
            "current data version differs from snapshot; replay is not reproducible",
            {"snapshot": session.data_version, "current": data_version},
        )
    if data_sha256 is not None and data_sha256 != session.data_sha256:
        _raise_mismatch(
            first_step,
            "data_sha256",
            "current data hash differs from snapshot; replay is not reproducible",
            {"snapshot": session.data_sha256, "current": data_sha256},
        )

    source = Path(data_path) if data_path is not None else Path(session.source_path)
    try:
        recomputed = evaluate_file(
            source,
            session.config,
            data_version=data_version if data_version is not None else session.data_version,
            force_path="auto",
        )
    except DomainError as exc:
        _raise_mismatch(
            first_step,
            "data_source",
            "cannot recompute snapshot from current data source",
            {"source": str(source), "cause_code": exc.code, "cause": exc.to_dict()},
        )
        raise AssertionError("unreachable") from exc

    if recomputed.data_sha256 != session.data_sha256:
        _raise_mismatch(
            first_step,
            "data_sha256",
            "current data content hash differs from snapshot; replay is not reproducible",
            {"snapshot": session.data_sha256, "current": recomputed.data_sha256},
        )
    if recomputed.data_version != session.data_version:
        _raise_mismatch(
            first_step,
            "data_version",
            "current data version differs from snapshot; replay is not reproducible",
        )
    if recomputed.row_count != session.row_count:
        _raise_mismatch(
            first_step,
            "row_count",
            "current data row count differs from snapshot",
            {"snapshot": session.row_count, "current": recomputed.row_count},
        )
    if recomputed.first_minute != session.first_minute:
        _raise_mismatch(first_step, "first_minute", "current data first minute differs from snapshot")
    if recomputed.last_minute != session.last_minute:
        _raise_mismatch(first_step, "last_minute", "current data last minute differs from snapshot")

    _compare_session(session, recomputed)
    elapsed_ns = time.perf_counter_ns() - started_ns
    return ReplayReport(
        verified=True,
        steps_checked=len(session.steps),
        windows_checked=len(session.long_windows) + len(session.short_windows),
        data_version_match=True,
        data_sha256_match=True,
        parameter_match=True,
        adaptive_path=recomputed.resource.adaptive_path,
        elapsed_ms=elapsed_ns // 1_000_000,
        elapsed_us=elapsed_ns // 1_000,
    )
