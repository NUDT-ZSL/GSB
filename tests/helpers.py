from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "samples" / "demo.json"


def demo_raw() -> dict[str, Any]:
    return json.loads(DEMO.read_text(encoding="utf-8"))


def make_bundle(
    *,
    resources: list[dict[str, Any]],
    holders: list[dict[str, Any]],
    applications: list[dict[str, Any]],
    budget: int = 1000,
    quota: int = 1,
    starvation: int = 2,
    step: int = 1000,
    labels: list[str] | None = None,
    initial_clock: int = 0,
    max_forced: int = 1,
    rollback_tolerance: int = 0,
) -> dict[str, Any]:
    return {
        "config": {
            "round_budget_ms": budget,
            "fair_quota_min_grants": quota,
            "starvation_rounds": starvation,
            "clock_step_ms": step,
            "fairness_labels": labels or ["gold", "silver", "bronze"],
            "initial_clock_ms": initial_clock,
            "max_forced_yields_per_application": max_forced,
            "clock_rollback_tolerance_ms": rollback_tolerance,
        },
        "resources": copy.deepcopy(resources),
        "holders": copy.deepcopy(holders),
        "applications": copy.deepcopy(applications),
    }


def resource(
    lock_key: str,
    *,
    duration: int = 500,
    generation: int = 1,
    priority: int = 50,
    max_wait: int = 8,
) -> dict[str, Any]:
    return {
        "lock_key": lock_key,
        "lease_duration_ms": duration,
        "lease_generation": generation,
        "priority": priority,
        "max_wait_rounds": max_wait,
    }


def holder(identity: str, cap: int = 2, heartbeat: int = 250) -> dict[str, Any]:
    return {
        "identity": identity,
        "heartbeat_ms": heartbeat,
        "session_generation": 1,
        "max_concurrent_grants": cap,
    }


def application(
    seq: int,
    holder_name: str,
    lock_key: str,
    *,
    arrival: int = 100,
    desired: int = 500,
    label: str = "gold",
    priority: int = 50,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    item = {
        "seq": seq,
        "holder": holder_name,
        "lock_key": lock_key,
        "logical_clock_ms": arrival,
        "desired_hold_ms": desired,
        "fairness_label": label,
        "priority": priority,
    }
    if expected_generation is not None:
        item["expected_lease_generation"] = expected_generation
    return item
