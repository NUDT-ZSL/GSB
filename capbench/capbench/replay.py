"""Post-recovery consistency replay (requirement 6).

Each finished stage recorded in the snapshot is re-driven against the local
mock service using its recorded concurrency/duration, re-aggregated with the
same aggregation caliber and re-judged against the snapshot's SLO. Results are
compared item by item under fixed tolerances.

Tolerances (fixed, documented, aggregation agg-v1):
  success_rate_bp : +/- 50 bp  (0.5pp; deterministic errors, jitter only from
                    scheduling/edge of duration)
  p95_latency_ms  : +/- 5 ms   (server process_ms is fixed; jitter is scheduling)
  throughput_rps  : +/- 10% relative, at least 2 rps
  verdict         : exact match
Any parameter-version or server-config mismatch is non-reproducible and is
reported with E_REPLAY, naming the first deviating stage.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .clock import MonotonicClock
from .driver import StageDriver
from .errors import ReplayError
from .metrics import aggregate, judge
from .models import SloThresholds, StageSpec
from .snapshot import Snapshot
from .versioning import fingerprint
from .scene import _parse_scene  # reuse strong parser for embedded scene

TOLERANCE = {
    "success_rate_bp_abs": 50,
    "p95_latency_ms_abs": 5,
    "throughput_rel_pct": 15,
    "throughput_min_abs": 3,
}


@dataclass
class ReplayItem:
    stage_index: int
    matched: bool
    deviations: list[str] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayReport:
    snapshot_name: str
    reproducible: bool
    items: list[ReplayItem]
    tolerance: dict[str, Any]


def _check_server_config(snapshot: Snapshot, live_config: dict[str, Any]) -> None:
    recorded = snapshot.server_config
    for key in ("process_ms", "error_rate_bp", "seed", "host", "port"):
        if recorded.get(key) != live_config.get(key):
            raise ReplayError(
                "target service configuration differs from snapshot; replay "
                "not reproducible",
                {
                    "first_deviating_stage": None,
                    "mismatched_key": key,
                    "snapshot_value": recorded.get(key),
                    "live_value": live_config.get(key),
                },
            )


def replay_snapshot(snapshot: Snapshot, live_server_config: dict[str, Any],
                    stop_event: threading.Event | None = None) -> ReplayReport:
    if stop_event is None:
        stop_event = threading.Event()
    _check_server_config(snapshot, live_server_config)

    scene = _parse_scene(snapshot.scene)
    live_fp = fingerprint(scene)
    if live_fp != snapshot.params_fingerprint:
        raise ReplayError(
            "load parameter fingerprint mismatch; replay not reproducible",
            {
                "first_deviating_stage": None,
                "snapshot_fingerprint": snapshot.params_fingerprint,
                "live_fingerprint": live_fp,
            },
        )

    specs = {s.index: s for s in scene.stages}
    items: list[ReplayItem] = []
    for recorded in sorted(snapshot.stage_results,
                           key=lambda r: r["stage_index"]):
        index = recorded["stage_index"]
        spec = specs[index]
        if (recorded["spec"]["concurrency"] != spec.concurrency
                or recorded["spec"]["duration_s"] != spec.duration_s
                or recorded["spec"]["throughput_limit_rps"]
                != spec.throughput_limit_rps):
            raise ReplayError(
                "recorded stage parameters inconsistent with snapshot scene",
                {"first_deviating_stage": index},
            )
        item = _replay_one(spec, scene.slo, recorded,
                           scene.sample_interval_ms, stop_event)
        items.append(item)
        if not item.matched:
            raise ReplayError(
                f"replay deviation at stage {index}",
                {"first_deviating_stage": index,
                 "deviations": item.deviations,
                 "tolerance": TOLERANCE},
            )
    return ReplayReport(snapshot.name, True, items, dict(TOLERANCE))


def _replay_one(spec: StageSpec, slo: SloThresholds, recorded: dict[str, Any],
                sample_interval_ms: int, stop_event) -> ReplayItem:
    clock = MonotonicClock()
    driver = StageDriver(
        host=recorded["target"]["host"],
        port=recorded["target"]["port"],
        spec=spec,
        sample_interval_ms=sample_interval_ms,
        clock=clock,
        stop_event=stop_event,
    )
    observation = driver.run()
    if observation is None:
        raise ReplayError("replay stage interrupted",
                          {"first_deviating_stage": spec.index})
    metrics = aggregate(observation)
    verdict = judge(metrics, slo)

    expected_m = recorded["metrics"]
    deviations: list[str] = []
    observed = {
        "success_rate_bp": metrics.success_rate_bp,
        "p95_latency_ms": metrics.p95_latency_ms,
        "throughput_rps": metrics.throughput_rps,
        "verdict": verdict.verdict,
    }
    expected = {
        "success_rate_bp": expected_m["success_rate_bp"],
        "p95_latency_ms": expected_m["p95_latency_ms"],
        "throughput_rps": expected_m["throughput_rps"],
        "verdict": recorded["verdict"],
    }
    if abs(metrics.success_rate_bp - expected_m["success_rate_bp"]) \
            > TOLERANCE["success_rate_bp_abs"]:
        deviations.append("success_rate_bp")
    if abs(metrics.p95_latency_ms - expected_m["p95_latency_ms"]) \
            > TOLERANCE["p95_latency_ms_abs"]:
        deviations.append("p95_latency_ms")
    diff = abs(metrics.throughput_rps - expected_m["throughput_rps"])
    allowed = max(TOLERANCE["throughput_min_abs"],
                  expected_m["throughput_rps"]
                  * TOLERANCE["throughput_rel_pct"] // 100)
    if diff > allowed:
        deviations.append("throughput_rps")
    if verdict.verdict != recorded["verdict"]:
        deviations.append("verdict")
    return ReplayItem(spec.index, not deviations, deviations,
                      observed, expected)



