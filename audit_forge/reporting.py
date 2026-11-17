from __future__ import annotations

from typing import Any, Iterable

from .engine import AuditEngine
from .store import LoadResult


def case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in case.items()
        if key != "evidence"
    }


def issue_severity(issue: dict[str, Any]) -> str:
    return issue.get("severity", "error")


def failures_from(
    engine: AuditEngine,
    load_result: LoadResult | None = None,
    *,
    include_samples: bool = True,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if load_result is not None:
        failures.extend(
            issue
            for issue in load_result.issues
            if issue_severity(issue) == "error"
        )
    if include_samples:
        failures.extend(engine.rejects.values())
        analysis = engine.analyze()
        failures.extend(
            anomaly
            for anomaly in analysis["anomalies"]
            if anomaly.get("severity", "error") == "error"
        )
    return failures


def report_status(
    failures: Iterable[dict[str, Any]],
    *,
    fatal: bool = False,
) -> tuple[str, int]:
    if fatal:
        return "error", 1
    if any(True for _ in failures):
        return "partial", 1
    return "ok", 0

