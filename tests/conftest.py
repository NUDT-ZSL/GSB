from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from flowguard.models import parse_sample


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fixed_raw() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return (
        read_json(SAMPLES / "topology.json"),
        read_json(SAMPLES / "events.json"),
        read_json(SAMPLES / "policy.json"),
    )


def fixed_sample():
    return parse_sample(*fixed_raw())


def simple_topology(*services: str) -> dict[str, Any]:
    names = services or ("svc-a",)
    return {
        "version": 1,
        "services": [
            {
                "name": name,
                "instances": [f"{name}-1"],
                "priority": index + 1,
            }
            for index, name in enumerate(names)
        ],
        "dependencies": [],
    }


def simple_policy(**overrides: Any) -> dict[str, Any]:
    policy = {
        "qps_limit": 100,
        "token_bucket_capacity": 10,
        "window_length_ms": 100,
        "window_step_ms": 20,
        "breaker_error_rate_threshold": 0.5,
        "breaker_min_requests": 3,
        "breaker_consecutive_failures": 3,
        "breaker_open_ms": 100,
        "half_open_max_probes": 2,
        "half_open_successes_required": 2,
        "degrade_level": 2,
        "degrade_allow_ratio": 0.5,
        "degrade_error_rate_threshold": 0.5,
        "degrade_stable_windows": 1,
        "retention_limit": 6,
        "clock_tolerance_ms": 5,
        "summary_bins": 16,
        "summary_quantile": 0.95,
        "mode_restore_ratio": 0.75,
    }
    policy.update(overrides)
    return {"version": 1, "default": policy, "services": {}}


def events_doc(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "events": events}


def event(
    seq: int,
    service: str = "svc-a",
    clock_ms: int = 0,
    result: str = "success",
    latency_ms: float = 10.0,
    cost_weight: float = 1.0,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "service": service,
        "clock_ms": clock_ms,
        "result": result,
        "latency_ms": latency_ms,
        "cost_weight": cost_weight,
    }


def make_sample(
    *,
    topology: dict[str, Any] | None = None,
    events: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
):
    return parse_sample(
        copy.deepcopy(topology or simple_topology()),
        copy.deepcopy(events or events_doc([event(1)])),
        copy.deepcopy(policy or simple_policy()),
    )


def write_json(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def fixed_db(tmp_path: Path):
    from flowguard import Adjudicator

    db = tmp_path / "fixed.db"
    adjudicator = Adjudicator(db)
    adjudicator.load_sample(fixed_sample(), reset=True)
    report = adjudicator.run()
    assert report["ok"] is True
    return db, adjudicator, report
