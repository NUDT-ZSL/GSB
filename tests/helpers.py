from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process_env = os.environ.copy()
    process_env.pop("TIERLAB_CRASH_AT", None)
    if env:
        process_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-m", "tierlab", *args],
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {
            "_stdout": completed.stdout,
            "_stderr": completed.stderr,
            "_returncode": completed.returncode,
        }
    if check and completed.returncode != 0:
        raise AssertionError(
            f"CLI failed: {completed.returncode}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed, payload


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def tiny_manifest(bucket: str = "tiny-a") -> dict[str, Any]:
    return {
        "bucket": bucket,
        "objects": [
            {
                "key": "hot-low",
                "size_bytes": 100,
                "last_access_clock": 0,
                "access_count": 0,
                "heat_weight": 1,
                "declared_tier": "hot",
                "migratable": True,
            },
            {
                "key": "cold-high",
                "size_bytes": 100,
                "last_access_clock": 0,
                "access_count": 20,
                "heat_weight": 100,
                "declared_tier": "cold",
                "migratable": True,
            },
        ],
    }


def tiny_config() -> dict[str, Any]:
    return {
        "hot_capacity_bytes": 300,
        "cold_capacity_bytes": 300,
        "round_byte_budget": 1000,
        "migration_concurrency": 1,
        "hot_threshold": "5",
        "clock_tolerance": 1000,
        "mode": "exact",
        "backoff": {
            "base_delay_clocks": 50,
            "max_delay_clocks": 200,
            "multiplier": 2,
            "max_consecutive_failures": 3,
        },
        "sampling": {
            "ratio": "0.5",
            "confidence_z": "1.96",
            "min_sample_per_stratum": 2,
            "random_seed": 3,
        },
        "adaptive": {
            "exact_max_objects": 100,
            "exact_max_candidates": 100,
            "max_estimated_error_ratio": "0.5",
            "exact_scan_unit_cost": "1",
            "exact_candidate_unit_cost": "0.05",
            "sampled_index_unit_cost": "0.1",
            "sampled_object_unit_cost": "0.2",
        },
    }
