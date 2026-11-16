"""Controlled, reproducible anomaly injection and structured logging."""

from __future__ import annotations

from typing import Any

from .engine import Engine
from .errors import E_REPLAY, E_SKEW, E_STATS, LabError


ANOMALY_CODES = {
    "skew": E_SKEW,
    "stats": E_STATS,
    "replay": E_REPLAY,
}


def run_anomaly(
    engine: Engine,
    anomaly: str,
    query_text: str | None = None,
    stats_version: str | None = None,
) -> dict[str, Any]:
    anomaly = anomaly.lower().strip()
    if anomaly not in ANOMALY_CODES:
        raise ValueError(f"unknown anomaly: {anomaly}")
    expected = ANOMALY_CODES[anomaly]
    try:
        if anomaly == "skew":
            engine.inject_skew_anomaly()
        elif anomaly == "stats":
            engine.inject_stats_anomaly(stats_version)
        else:
            engine.inject_replay_anomaly(query_text)
        raise LabError(
            expected,
            "anomaly injection was expected to fail but did not",
            {"anomaly": anomaly},
        )
    except LabError as exc:
        if exc.code != expected:
            raise
        payload = {
            "anomaly": anomaly,
            "business_code": exc.code,
            "message": exc.message,
            "details": exc.details,
        }
        log = engine.record_log(f"anomaly.{anomaly}", payload)
        return {
            "ok": False,
            "anomaly": anomaly,
            "business_code": exc.code,
            "message": exc.message,
            "details": exc.details,
            "log_event_id": log["event_id"],
            "logged": True,
        }


def run_all_anomalies(
    engine: Engine,
    query_text: str | None = None,
    stats_version: str | None = None,
) -> list[dict[str, Any]]:
    return [
        run_anomaly(engine, anomaly, query_text=query_text, stats_version=stats_version)
        for anomaly in ("skew", "stats", "replay")
    ]
