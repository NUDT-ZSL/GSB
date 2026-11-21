from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from elasticctl.service import load_database

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sample_payload(name: str) -> Any:
    return read_json(SAMPLES / name)


def load_fixture(
    db_file: Path,
    *,
    workloads: Any | None = None,
    metrics: Any | None = None,
    rules: Any | None = None,
    budget: Any | None = None,
    samples: Any | None = None,
    outcomes: Any | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="elastic-fixture-") as temp:
        temp_path = Path(temp)
        paths = {
            "workloads_path": temp_path / "workloads.json",
            "metrics_path": temp_path / "metrics.json",
            "rules_path": temp_path / "rules.json",
            "budget_path": temp_path / "budget.json",
            "samples_path": temp_path / "samples.json",
            "outcomes_path": temp_path / "outcomes.json",
        }
        write_json(
            paths["workloads_path"],
            sample_payload("workloads.json") if workloads is None else workloads,
        )
        write_json(
            paths["metrics_path"],
            sample_payload("metric_sources.json") if metrics is None else metrics,
        )
        write_json(
            paths["rules_path"],
            sample_payload("scaling_rules.json") if rules is None else rules,
        )
        write_json(
            paths["budget_path"],
            sample_payload("resource_budget.json") if budget is None else budget,
        )
        write_json(
            paths["samples_path"],
            sample_payload("metric_stream.json") if samples is None else samples,
        )
        write_json(
            paths["outcomes_path"],
            sample_payload("action_outcomes.json") if outcomes is None else outcomes,
        )
        return load_database(
            db_file,
            replace=True,
            **paths,
        )


def run_cli(
    *args: str,
    db_file: Path,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.pop("ELASTICCTL_FAULT", None)
    if env:
        process_env.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "elasticctl",
            *args,
            "--db",
            str(db_file),
        ],
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def status_snapshot(db_file: Path) -> dict[str, Any]:
    from elasticctl.service import status

    return status(db_file)


def verify_database(db_file: Path) -> dict[str, Any]:
    from elasticctl.service import verify

    return verify(db_file)

