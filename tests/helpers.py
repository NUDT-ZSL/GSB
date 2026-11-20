from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from schedkernel.service import SchedulerService


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def service_for(tmp: Path, name: str = "scheduler") -> SchedulerService:
    return SchedulerService(
        tmp / f"{name}.db",
        tmp / f"{name}.lock",
        tmp / "tmp",
    )


def cli_service(tmp: Path) -> SchedulerService:
    return SchedulerService(tmp / "db.sqlite", tmp / "db.lock", tmp / "tmp")


def load_sample(service: SchedulerService, replica_name: str = "replicas.json") -> dict[str, Any]:
    return service.load_files(
        SAMPLES / "model.json",
        SAMPLES / replica_name,
        SAMPLES / "requests.json",
        SAMPLES / "precision.json",
    )


def run_cli(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "schedkernel", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def parse_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def parse_stderr(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stderr)

