from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def cli(
    *args: str,
    cwd: Path = ROOT,
    expect_json: bool = True,
) -> tuple[int, dict[str, Any] | None, str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    completed = subprocess.run(
        [sys.executable, "-m", "drillbench", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )
    payload = None
    if expect_json and completed.stdout.strip():
        payload = json.loads(completed.stdout)
    return completed.returncode, payload, completed.stdout, completed.stderr


def minimal_documents(
    *,
    steps: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    windows: list[dict[str, int]],
    injections: list[dict[str, Any]] | None = None,
    tick_logical_ms: int = 50,
    tick_real_ms: int = 10,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan_name = "fixture"
    plans = {
        "schema_version": 1,
        "plans": [{"name": plan_name, "steps": steps}],
    }
    services = {
        "schema_version": 1,
        "plans": [{"name": plan_name, "routes": routes}],
    }
    clock = {
        "schema_version": 1,
        "plans": [
            {
                "name": plan_name,
                "start_ms": 0,
                "tick": {
                    "logical_ms": tick_logical_ms,
                    "real_ms": tick_real_ms,
                },
                "step_windows": windows,
            }
        ],
    }
    injection_doc = {
        "schema_version": 1,
        "plans": [
            {
                "name": plan_name,
                "injections": injections or [],
            }
        ],
    }
    return plans, services, clock, injection_doc


def write_documents(
    root: Path,
    documents: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]],
) -> tuple[Path, Path, Path, Path]:
    paths = (
        root / "plans.json",
        root / "services.json",
        root / "clock.json",
        root / "injections.json",
    )
    for path, document in zip(paths, documents):
        write_json(path, document)
    return paths


def make_step(
    seq: int,
    *,
    depends_on: list[int] | None = None,
    timeout: int = 400,
    skippable: bool = False,
    expected: str = "HTTP_2XX",
) -> dict[str, Any]:
    return {
        "seq": seq,
        "name": f"step-{seq}",
        "depends_on": depends_on or [],
        "expected_event": expected,
        "timeout_logical_ms": timeout,
        "skippable": skippable,
    }


def make_route(
    seq: int,
    *,
    path: str | None = None,
    status: int = 200,
    delay: int = 50,
    body: str = '{"ok":true}',
    content_type: str = "application/json",
    mode: str = "normal",
) -> dict[str, Any]:
    return {
        "step": seq,
        "path": path or f"/step/{seq}",
        "status": status,
        "delay_logical_ms": delay,
        "body": body,
        "content_type": content_type,
        "mode": mode,
    }


def make_window(seq: int, start: int, timeout: int) -> dict[str, int]:
    return {
        "step": seq,
        "start_ms": start,
        "end_ms": start + timeout,
    }

