from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pytest

from netdiag.mock_service import LocalMockService, load_mock_config

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_SCENARIO = ROOT / "samples" / "scenario_demo.json"
SAMPLE_MOCK = ROOT / "samples" / "mock_service.json"


@pytest.fixture
def mock_service() -> Iterable[LocalMockService]:
    service = LocalMockService(load_mock_config(SAMPLE_MOCK), port=0).start()
    try:
        yield service
    finally:
        service.stop()


def sample_payload(
    *,
    port: int,
    rounds: int | None = None,
    config_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = json.loads(SAMPLE_SCENARIO.read_text(encoding="utf-8"))
    for target in payload["targets"]:
        target["endpoint"] = target["endpoint"].replace("127.0.0.1:18080", f"127.0.0.1:{port}")
    if rounds is not None:
        payload["probe_script"]["rounds"] = payload["probe_script"]["rounds"][:rounds]
        payload["probe_script"]["clock_step_table"] = (
            payload["probe_script"]["clock_step_table"][:rounds]
        )
    if config_updates:
        payload["config"].update(config_updates)
    return payload


def write_scenario(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def cli_env(crash_at: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("NETDIAG_TEST_CRASH_AT", None)
    if crash_at:
        env["NETDIAG_TEST_CRASH_AT"] = crash_at
    return env


def run_cli(
    args: list[str],
    *,
    crash_at: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", "netdiag", *args],
        cwd=ROOT,
        env=cli_env(crash_at),
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"CLI failed with {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def popen_cli(
    args: list[str],
    *,
    crash_at: str | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "netdiag", *args],
        cwd=ROOT,
        env=cli_env(crash_at),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
