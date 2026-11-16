#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.reconciliation import build_markdown, compare_summaries  # noqa: E402

ARTIFACTS = ROOT / "artifacts" / "consistency"
RUNS = [
    {
        "name": "run-a",
        "concurrency": 1,
        "delay_profile": "slow",
        "fault_profile": "mixed",
    },
    {
        "name": "run-b",
        "concurrency": 6,
        "delay_profile": "bursty",
        "fault_profile": "mixed",
    },
]
COMMON = {
    "render_timeout_s": 4.0,
    "max_render_observations": 60,
    "max_retries": 4,
    "backoff_base_ms": 60,
    "backoff_max_ms": 600,
    "high_latency_ms": 500,
    "max_session_replays": 6,
    "request_timeout_s": 10,
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # pragma: no cover - diagnostic path
            last_error = exc
        time.sleep(0.08)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def start_server(db_path: Path, port: int) -> tuple[subprocess.Popen, Path]:
    log_path = ARTIFACTS / f"server-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "AUDIT_DB_PATH": str(db_path),
            "LOG_LEVEL": "warning",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    wait_health(f"http://127.0.0.1:{port}")
    return process, log_path


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def create_and_wait(
    client: httpx.Client,
    *,
    name: str,
    wave: str,
    run: dict[str, Any],
    timeout: float = 120.0,
) -> dict[str, Any]:
    payload = {
        "name": name,
        "wave": wave,
        "concurrency": run["concurrency"],
        "delay_profile": run["delay_profile"],
        "fault_profile": run["fault_profile"],
        **COMMON,
    }
    response = client.post("/api/tasks", json=payload)
    response.raise_for_status()
    task_id = response.json()["task_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/tasks/{task_id}").json()
        status = detail["task"]["status"]
        if status not in {"created", "running"}:
            if status != "completed":
                raise RuntimeError(
                    f"task {task_id} ended as {status}: "
                    f"{detail['task'].get('task_code')} {detail['task'].get('error')}"
                )
            return detail
        time.sleep(0.1)
    raise RuntimeError(f"task {task_id} did not finish in {timeout}s")


def run_configuration(run: dict[str, Any]) -> dict[str, Any]:
    db_path = ARTIFACTS / f"{run['name']}.sqlite3"
    if db_path.exists():
        db_path.unlink()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    process, log_path = start_server(db_path, port)
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            baseline = create_and_wait(client, name=f"{run['name']}-baseline", wave="baseline", run=run)
            incremental = create_and_wait(
                client, name=f"{run['name']}-incremental", wave="incremental", run=run
            )
        summary = Database(db_path).global_consistency_summary()
        return {
            **run,
            "port": port,
            "base_url": base_url,
            "db_path": str(db_path),
            "log_path": str(log_path),
            "task_ids": [baseline["task"]["id"], incremental["task"]["id"]],
            "task_summaries": [baseline["task"]["summary"], incremental["task"]["summary"]],
            "summary": summary,
        }
    finally:
        stop_server(process)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    runs = []
    for run in RUNS:
        print(f"running {run['name']}: concurrency={run['concurrency']} delay={run['delay_profile']} ...")
        runs.append(run_configuration(run))
        summary = runs[-1]["summary"]
        print(
            f"  businesses={summary['business_count']} records={summary['record_count']} "
            f"edges={summary['edge_count']}"
        )
    comparison = compare_summaries(runs[0]["summary"], runs[1]["summary"])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": [{key: value for key, value in run.items() if key != "summary"} for run in runs],
        "comparison": {key: value for key, value in comparison.items() if key not in {"left", "right"}},
        "identical": comparison["identical"],
    }
    report_path = ARTIFACTS / "reconciliation.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = build_markdown(report)
    (ARTIFACTS / "reconciliation.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"artifacts: {report_path}")
    return 0 if comparison["identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
