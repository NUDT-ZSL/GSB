from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]


def base_config() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": "test-policy-v1",
        "topology": {
            "services": [
                {
                    "name": "svc-a",
                    "instance_id": "a-1",
                    "upstream": [],
                    "priority": 1,
                    "fault_domain": "z1",
                },
                {
                    "name": "svc-b",
                    "instance_id": "b-1",
                    "upstream": ["svc-a"],
                    "priority": 1,
                    "fault_domain": "z2",
                },
            ]
        },
        "rate_limit": {
            "clock_rollback_tolerance_ms": 5,
            "services": {
                "svc-a": {
                    "capacity": 10,
                    "refill_per_sec": 10,
                    "burst_depth": 5,
                },
                "svc-b": {
                    "capacity": 10,
                    "refill_per_sec": 10,
                    "burst_depth": 5,
                },
            },
            "global": {
                "capacity": 100,
                "refill_per_sec": 100,
                "burst_depth": 20,
            },
        },
        "circuit_breaker": {
            "window_length_ms": 1000,
            "step_ms": 250,
            "failure_rate_threshold": 0.5,
            "half_open_probe_count": 2,
            "cooldown_ms": 500,
        },
        "degradation": {
            "levels": [
                {"name": "L2", "ratio": 0.25},
                {"name": "L1", "ratio": 0.5},
                {"name": "NORMAL", "ratio": 1.0},
            ],
            "initial": "NORMAL",
            "recovery_ladder": ["L2", "L1", "NORMAL"],
            "recovery_successes_per_step": 2,
        },
        "global_error_budget": {
            "max_failures": 100,
            "window_ms": 5000,
        },
    }


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def event(
    seq: int,
    service: str,
    clock_ms: int,
    success: bool = True,
    *,
    latency_us: int = 1000,
    inject: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "seq": seq,
        "service": service,
        "clock_ms": clock_ms,
        "result": "success" if success else "failure",
        "latency_us": latency_us,
        "inject": inject or [],
    }


def write_sample(
    directory: Path,
    config: Dict[str, Any],
    events: Iterable[Dict[str, Any]],
    anomalies: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.json"
    events_path = directory / "events.json"
    anomalies_path = directory / "anomalies.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    events_path.write_text(
        json.dumps({"events": list(events)}, indent=2),
        encoding="utf-8",
    )
    anomalies_path.write_text(
        json.dumps({"anomalies": list(anomalies or [])}, indent=2),
        encoding="utf-8",
    )
    return {
        "config": config_path,
        "events": events_path,
        "anomalies": anomalies_path,
    }


def run_cli(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sre_pipeline", *args],
        cwd=str(cwd or ROOT),
        text=True,
        capture_output=True,
        check=False,
    )


def parse_stdout(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def parse_stderr(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stderr.strip():
        return None
    return json.loads(result.stderr)

