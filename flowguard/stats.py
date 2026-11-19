"""Exact and layered sliding-window statistics."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import ErrorCode, FlowGuardError
from .models import Event, Policy
from .util import canonical_json, latency_bin, latency_bin_bounds, sha256_json


@dataclass
class SegmentData:
    service: str
    segment_id: int
    event_count: int = 0
    error_count: int = 0
    success_count: int = 0
    allowed_count: int = 0
    cost_units: int = 0
    max_latency_ms: float = 0.0
    histogram: np.ndarray = field(
        default_factory=lambda: np.zeros(1, dtype=np.int64)
    )
    last_seq: int = 0
    version: str = ""

    @classmethod
    def empty(cls, service: str, segment_id: int, bins: int) -> "SegmentData":
        return cls(
            service=service,
            segment_id=segment_id,
            histogram=np.zeros(bins, dtype=np.int64),
        )

    @classmethod
    def from_row(cls, row: sqlite3.Row, bins: int) -> "SegmentData":
        histogram_raw = json.loads(row["histogram_json"])
        histogram = np.asarray(histogram_raw, dtype=np.int64)
        if histogram.shape != (bins,):
            raise FlowGuardError(
                ErrorCode.STATE_INCONSISTENT,
                "persisted histogram width does not match policy",
                {
                    "service": row["service"],
                    "segment_id": row["segment_id"],
                    "expected_bins": bins,
                    "actual_bins": int(histogram.size),
                },
            )
        return cls(
            service=row["service"],
            segment_id=int(row["segment_id"]),
            event_count=int(row["event_count"]),
            error_count=int(row["error_count"]),
            success_count=int(row["success_count"]),
            allowed_count=int(row["allowed_count"]),
            cost_units=int(row["cost_units"]),
            max_latency_ms=float(row["max_latency_ms"]),
            histogram=histogram,
            last_seq=int(row["last_seq"]),
            version=str(row["version"]),
        )

    def add(self, event: Event, bins: int) -> None:
        self.event_count += 1
        self.allowed_count += 1
        if event.result == "error":
            self.error_count += 1
        else:
            self.success_count += 1
        self.cost_units += event.cost_units
        self.max_latency_ms = max(self.max_latency_ms, event.latency_ms)
        self.histogram[latency_bin(event.latency_ms, bins)] += 1
        self.last_seq = max(self.last_seq, event.seq)

    def payload(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "segment_id": self.segment_id,
            "event_count": self.event_count,
            "error_count": self.error_count,
            "success_count": self.success_count,
            "allowed_count": self.allowed_count,
            "cost_units": self.cost_units,
            "max_latency_ms": self.max_latency_ms,
            "histogram": [int(value) for value in self.histogram.tolist()],
            "last_seq": self.last_seq,
        }

    def finalize_version(self) -> str:
        self.version = sha256_json(self.payload())
        return self.version


@dataclass
class WindowComputation:
    service: str
    window_id: int
    window_version: str
    stats_mode: str
    previous_mode: str
    switch_trigger: str
    switch_basis: dict[str, Any]
    event_count: int
    error_count: int
    success_count: int
    error_rate: float
    quantile: float
    quantile_estimate: float
    quantile_lower: float
    quantile_upper: float
    error_rate_upper_bound: float
    latency_error_upper_bound_ms: float
    active_segments: list[dict[str, Any]]
    segments: dict[int, SegmentData]
    payload: dict[str, Any]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "window_version": self.window_version,
            "stats_mode": self.stats_mode,
            "mode_switch": {
                "from": self.previous_mode,
                "to": self.stats_mode,
                "switched": self.previous_mode != self.stats_mode,
                "trigger": self.switch_trigger,
                "basis": self.switch_basis,
            },
            "event_count": self.event_count,
            "valid_event_count": self.event_count,
            "error_count": self.error_count,
            "success_count": self.success_count,
            "error_rate": self.error_rate,
            "error_rate_upper_bound": self.error_rate_upper_bound,
            "quantile": self.quantile,
            "quantile_estimate_ms": self.quantile_estimate,
            "quantile_lower_ms": self.quantile_lower,
            "quantile_upper_ms": self.quantile_upper,
            "latency_error_upper_bound_ms": self.latency_error_upper_bound_ms,
            "error_upper_bound": self.latency_error_upper_bound_ms,
            "active_segments": self.active_segments,
        }


def load_active_segments(
    conn: sqlite3.Connection,
    service: str,
    policy: Policy,
    current_segment_id: int,
) -> dict[int, SegmentData]:
    first_segment = current_segment_id - policy.active_segment_count + 1
    rows = conn.execute(
        """
        SELECT snapshots.*
        FROM segment_heads AS heads
        JOIN segment_snapshots AS snapshots
          ON snapshots.service = heads.service
         AND snapshots.segment_id = heads.segment_id
         AND snapshots.version = heads.version
        WHERE heads.service = ?
          AND heads.segment_id BETWEEN ? AND ?
        ORDER BY heads.segment_id
        """,
        (service, first_segment, current_segment_id),
    ).fetchall()
    return {
        int(row["segment_id"]): SegmentData.from_row(row, policy.summary_bins)
        for row in rows
    }


def rollover_segment(
    service: str,
    policy: Policy,
    current_segment_id: int,
    segments: dict[int, SegmentData],
) -> dict[int, SegmentData]:
    first_segment = current_segment_id - policy.active_segment_count + 1
    return {
        segment_id: segment
        for segment_id, segment in segments.items()
        if first_segment <= segment_id <= current_segment_id
    }


def compute_window_computation(
    conn: sqlite3.Connection,
    service: str,
    policy: Policy,
    event: Event | None,
    current_segment_id: int,
    previous_mode: str,
    include_event: bool,
) -> WindowComputation:
    segments = load_active_segments(conn, service, policy, current_segment_id)
    segments = rollover_segment(service, policy, current_segment_id, segments)
    if include_event:
        if event is None:
            raise FlowGuardError(
                ErrorCode.STATE_INCONSISTENT,
                "event is required when include_event is true",
                {"service": service},
            )
        segment = segments.get(current_segment_id)
        if segment is None:
            segment = SegmentData.empty(service, current_segment_id, policy.summary_bins)
        segment.add(event, policy.summary_bins)
        segment.finalize_version()
        segments[current_segment_id] = segment

    event_count = sum(segment.event_count for segment in segments.values())
    error_count = sum(segment.error_count for segment in segments.values())
    success_count = sum(segment.success_count for segment in segments.values())
    if event_count <= 0:
        raise FlowGuardError(
            ErrorCode.ZERO_VALID_REQUESTS,
            "window contains zero valid requests",
            {"service": service, "window_id": current_segment_id},
        )

    stats_mode, trigger, basis = choose_stats_mode(
        event_count,
        previous_mode,
        policy.retention_limit,
        policy.restore_threshold,
    )
    if stats_mode == "exact":
        latencies = exact_latencies(
            conn,
            service,
            policy,
            current_segment_id,
            event if include_event else None,
        )
        quantile_value = float(
            np.quantile(
                np.asarray(latencies, dtype=np.float64),
                policy.summary_quantile,
                method="linear",
            )
        )
        quantile_lower = quantile_value
        quantile_upper = quantile_value
        latency_error = 0.0
    else:
        try:
            quantile_value, quantile_lower, quantile_upper = layered_quantile(
                segments,
                policy.summary_quantile,
                policy.summary_bins,
            )
            latency_error = quantile_upper - quantile_lower
        except Exception as exc:
            raise FlowGuardError(
                ErrorCode.MODE_SWITCH_FAILED,
                "failed to switch to layered summary mode",
                {
                    "service": service,
                    "window_id": current_segment_id,
                    "event_count": event_count,
                    "reason": str(exc),
                },
            ) from exc

    error_rate = error_count / event_count
    active_segments = [
        {
            "segment_id": segment_id,
            "version": segment.version,
            "event_count": segment.event_count,
        }
        for segment_id, segment in sorted(segments.items())
    ]
    payload: dict[str, Any] = {
        "service": service,
        "window_id": current_segment_id,
        "stats_mode": stats_mode,
        "event_count": event_count,
        "error_count": error_count,
        "success_count": success_count,
        "error_rate": error_rate,
        "quantile": policy.summary_quantile,
        "quantile_estimate_ms": quantile_value,
        "quantile_lower_ms": quantile_lower,
        "quantile_upper_ms": quantile_upper,
        "error_rate_upper_bound": 0.0,
        "latency_error_upper_bound_ms": latency_error,
        "retention_limit": policy.retention_limit,
        "restore_threshold": policy.restore_threshold,
        "switch_trigger": trigger,
        "switch_basis": basis,
        "active_segments": active_segments,
    }
    version = sha256_json(payload)
    payload["window_version"] = version
    return WindowComputation(
        service=service,
        window_id=current_segment_id,
        window_version=version,
        stats_mode=stats_mode,
        previous_mode=previous_mode,
        switch_trigger=trigger,
        switch_basis=basis,
        event_count=event_count,
        error_count=error_count,
        success_count=success_count,
        error_rate=error_rate,
        quantile=policy.summary_quantile,
        quantile_estimate=quantile_value,
        quantile_lower=quantile_lower,
        quantile_upper=quantile_upper,
        error_rate_upper_bound=0.0,
        latency_error_upper_bound_ms=latency_error,
        active_segments=active_segments,
        segments=segments,
        payload=payload,
    )


def choose_stats_mode(
    event_count: int,
    previous_mode: str,
    retention_limit: int,
    restore_threshold: int,
) -> tuple[str, str, dict[str, Any]]:
    basis = {
        "window_event_count": event_count,
        "retention_limit": retention_limit,
        "restore_threshold": restore_threshold,
        "previous_mode": previous_mode,
    }
    if event_count > retention_limit:
        return "layered", "window_count_exceeds_retention_limit", basis
    if previous_mode == "layered" and event_count > restore_threshold:
        return "layered", "hysteresis_retains_layered_mode", basis
    if previous_mode == "layered":
        return "exact", "window_count_at_or_below_restore_threshold", basis
    return "exact", "window_count_within_retention_limit", basis


def exact_latencies(
    conn: sqlite3.Connection,
    service: str,
    policy: Policy,
    current_segment_id: int,
    current_event: Event | None,
) -> list[float]:
    first_segment = current_segment_id - policy.active_segment_count + 1
    rows = conn.execute(
        """
        SELECT latency_ms
        FROM window_events
        WHERE service = ?
          AND segment_id BETWEEN ? AND ?
          AND allowed = 1
        ORDER BY seq
        """,
        (service, first_segment, current_segment_id),
    ).fetchall()
    values = [float(row["latency_ms"]) for row in rows]
    if current_event is not None:
        values.append(current_event.latency_ms)
    if not values:
        raise FlowGuardError(
            ErrorCode.ZERO_VALID_REQUESTS,
            "window contains zero valid requests for exact statistics",
            {"service": service, "window_id": current_segment_id},
        )
    return values


def layered_quantile(
    segments: dict[int, SegmentData],
    quantile: float,
    bins: int,
) -> tuple[float, float, float]:
    total = sum(int(segment.histogram.sum()) for segment in segments.values())
    if total <= 0:
        raise FlowGuardError(
            ErrorCode.ZERO_VALID_REQUESTS,
            "layered summary contains zero valid requests",
        )
    histogram = np.zeros(bins, dtype=np.int64)
    for segment in segments.values():
        histogram += segment.histogram
    target = max(1, int(np.ceil(quantile * total)))
    cumulative = np.cumsum(histogram)
    indexes = np.nonzero(cumulative >= target)[0]
    if indexes.size == 0:
        index = bins - 1
    else:
        index = int(indexes[0])
    lower, upper = latency_bin_bounds(index, bins)
    estimate = (lower + upper) / 2.0
    return estimate, lower, upper
