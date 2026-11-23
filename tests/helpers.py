from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from gateway.engine import Engine, build_genesis
from gateway.validation import normalize_inputs

ROOT = Path(__file__).resolve().parents[1]


def service_doc(
    name: str = "svc",
    *,
    replicas: list[dict[str, Any]] | None = None,
    batch_window_ms: int = 1,
) -> dict[str, Any]:
    if replicas is None:
        replicas = [{"replica_id": "r1", "slots": 2, "compute_units": 1.0}]
    return {
        "schema_version": 1,
        "services": [
            {
                "name": name,
                "model_version": "m1",
                "batch_window_ms": batch_window_ms,
                "replicas": replicas,
            }
        ],
    }


def request_doc(
    requests: list[dict[str, Any]],
) -> dict[str, Any]:
    return {"schema_version": 1, "requests": requests}


def request(
    request_id: int,
    *,
    service: str = "svc",
    arrival_ms: int = 0,
    input_tokens: int = 10,
    output_tokens: int = 20,
    priority: int = 1,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "service": service,
        "arrival_ms": arrival_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "priority": priority,
    }


def config_doc(
    *,
    config_version: int = 1,
    capacity: int = 100,
    rate: int = 100,
    max_inflight: int = 100,
    round_budget_ms: int = 100,
    dispatch_timeout_ms: int = 5,
    base_ms: int = 2,
    cap_ms: int = 8,
    max_generation: int = 3,
    scale: int = 1000,
    tolerance_ms: int = 1,
    priority_min: int = 0,
    priority_max: int = 9,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "config_version": config_version,
        "global": {
            "capacity": capacity,
            "refill_rate_per_second": rate,
            "max_inflight": max_inflight,
        },
        "service": {
            "capacity": capacity,
            "refill_rate_per_second": rate,
            "max_inflight": max_inflight,
        },
        "service_overrides": {},
        "round_budget_ms": round_budget_ms,
        "dispatch_timeout_ms": dispatch_timeout_ms,
        "backoff": {
            "base_ms": base_ms,
            "cap_ms": cap_ms,
            "max_generation": max_generation,
        },
        "fixed_point": {"scale": scale, "tolerance_ms": tolerance_ms},
        "scheduling": {
            "priority_min": priority_min,
            "priority_max": priority_max,
        },
    }


def make_inputs(
    services: dict[str, Any],
    requests: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    return normalize_inputs(services, requests, config)


def make_engine(
    services: dict[str, Any],
    requests: dict[str, Any],
    config: dict[str, Any],
) -> tuple[Engine, dict[str, Any]]:
    inputs = make_inputs(services, requests, config)
    state = build_genesis(inputs)
    return Engine(state), state


def round_command(
    round_id: str,
    clock_ms: int,
    *,
    budget_ms: int | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    abort_overdue: bool = False,
    dispatch_enabled: bool = True,
    recover_only: bool = False,
    request_ids: list[int] | None = None,
    force_targets: dict[str, str] | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "round_id": round_id,
        "kind": "dispatch",
        "clock_ms": clock_ms,
        "abort_overdue": abort_overdue,
        "dispatch": dispatch_enabled,
        "recover_only": recover_only,
    }
    if budget_ms is not None:
        command["budget_ms"] = budget_ms
    if outcomes is not None:
        command["outcomes"] = outcomes
    if request_ids is not None:
        command["requests"] = request_ids
    if force_targets is not None:
        command["force_targets"] = force_targets
    return command


def write_json(path: Path, value: Any) -> Path:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2), encoding="utf-8"
    )
    return path


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    full_env.pop("GW_FAILPOINT", None)
    full_env.pop("GW_DEBUG", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "gateway", *args],
        cwd=str(cwd),
        env=full_env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def parse_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    text = completed.stdout.strip()
    if not text:
        raise AssertionError(
            f"no JSON output; returncode={completed.returncode}, stderr={completed.stderr}"
        )
    return json.loads(text)


def load_cli(
    db: Path,
    services: dict[str, Any],
    requests: dict[str, Any],
    config: dict[str, Any],
    *,
    clock_steps: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    base = db.parent
    services_path = write_json(base / "services.json", services)
    requests_path = write_json(base / "requests.json", requests)
    config_path = write_json(base / "config.json", config)
    args = [
        "load",
        "--db",
        str(db),
        "--services",
        str(services_path),
        "--requests",
        str(requests_path),
        "--config",
        str(config_path),
    ]
    if clock_steps is not None:
        steps_path = write_json(base / "clock_steps.json", clock_steps)
        args.extend(["--clock-steps", str(steps_path)])
    return run_cli(*args)


class TempDirTestCaseMixin:
    def make_temp_dir(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Path(temp.name)
