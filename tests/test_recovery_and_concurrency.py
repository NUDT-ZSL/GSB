from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from metric_engine.engine import Engine

from conftest import DEFAULT_PARTITIONS, DEFAULT_TOKENS, event


ROOT = Path(__file__).resolve().parents[1]


def _cli(
    workspace: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "metric_engine",
            "--compact",
            "--workspace",
            str(workspace),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )


def _load_cli(workspace, paths) -> None:
    result = _cli(
        workspace.runtime,
        "load",
        "--streams",
        str(paths["streams"]),
        "--partitions",
        str(paths["partitions"]),
        "--quotas",
        str(paths["quotas"]),
        "--tokens",
        str(paths["tokens"]),
    )
    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.parametrize(
    ("failpoint", "exit_code"),
    [
        ("after_data_append_before_commit", 86),
        ("after_ledger_charge_before_commit", 87),
    ],
)
def test_forced_termination_recovery_redoes_missing_ledger_once(
    workspace, failpoint: str, exit_code: int
) -> None:
    engine, paths = workspace.create(
        partitions={**DEFAULT_PARTITIONS, "partition_count": 2}
    )
    event_path = workspace.write_events(
        "recovery-events.json",
        [
            event(sample_seq=1, event_id="r1"),
            event(sample_seq=2, event_id="r2"),
        ],
    )
    report = engine.ingest_file(event_path)
    assert report["count"] == 2

    crashed = _cli(
        workspace.runtime,
        "flush",
        "--clock-ms",
        "1000",
        env_extra={"METRIC_ENGINE_FAILPOINT": failpoint},
    )
    assert crashed.returncode == exit_code, crashed.stderr + crashed.stdout

    reopened = Engine.open(workspace.runtime)
    resume = reopened.resume()
    assert resume["redone_samples"] == 1
    status = reopened.status()
    expected_used = 4
    assert sum(item["used_units_total"] for item in status["tenants"]) == expected_used * 1000
    conn = reopened.store._connect()
    try:
        quota_charges = conn.execute("SELECT COUNT(*) AS n FROM quota_charges").fetchone()["n"]
        token_charges = conn.execute("SELECT COUNT(*) AS n FROM token_charges").fetchone()["n"]
    finally:
        conn.close()
    assert quota_charges == 1
    assert token_charges == 1

    reopened.flush(clock_ms=1000)
    verification = reopened.verify()
    assert verification["persisted_samples"] == 2
    assert verification["errors"] == []
    assert verification["manifest"][0]["state"] == "PERSISTED"


def test_concurrent_duplicate_ingest_and_parallel_flush_are_consistent(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": f"metric.{index}",
            "tags": {"host": f"h{index}"},
            "sample_period_ms": 500,
            "sample_bytes": 4,
        }
        for index in range(4)
    ]
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "1000",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            }
        },
    }
    tokens = {
        **DEFAULT_TOKENS,
        "capacity_tokens": 100,
        "refill_rate_per_second": 100,
        "clock_step_ms": 100,
    }
    partitions = {
        **DEFAULT_PARTITIONS,
        "buffer_per_partition": 10,
        "flush_round_limit": 2,
        "round_time_budget_ms": 10000,
        "partition_count": 4,
    }
    _engine, paths = workspace.create(
        streams=streams,
        quotas=quotas,
        tokens=tokens,
        partitions=partitions,
    )
    _load_cli(workspace, paths)
    events = [
        {
            "tenant": "tenant-a",
            "metric": f"metric.{index}",
            "logical_time_ms": 1000,
            "sample_seq": 1,
            "event_id": f"c{index}",
        }
        for index in range(4)
    ]
    event_path = workspace.write_events("concurrent.json", events)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "metric_engine",
                "--compact",
                "--workspace",
                str(workspace.runtime),
                "ingest",
                "--events",
                str(event_path),
                "--no-auto-flush",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr + stdout

    engine = Engine.open(workspace.runtime)
    conn = engine.store._connect()
    try:
        sample_count = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
        duplicate_decisions = conn.execute(
            "SELECT COUNT(*) AS n FROM ingest_decisions WHERE decision='IDEMPOTENT'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert sample_count == 4
    assert duplicate_decisions >= 12
    assert Engine.open(workspace.runtime).status()["buffer_depth"] == 4

    flush_processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "metric_engine",
                "--compact",
                "--workspace",
                str(workspace.runtime),
                "flush",
                "--clock-ms",
                "2000",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    for process in flush_processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr + stdout

    engine = Engine.open(workspace.runtime)
    verification = engine.verify()
    assert verification["ok"] is True
    assert verification["persisted_samples"] == 4
    assert verification["buffered_samples"] == 0
    assert sum(
        item["used_units_total"] for item in engine.status()["tenants"]
    ) == 16000


def test_cli_verify_and_stable_status(workspace) -> None:
    engine, paths = workspace.create()
    _load_cli(workspace, paths)
    event_path = workspace.write_events("cli-events.json", [event(event_id="cli")])
    result = _cli(workspace.runtime, "ingest", "--events", str(event_path))
    assert result.returncode == 0, result.stderr + result.stdout
    result = _cli(workspace.runtime, "flush", "--clock-ms", "1000")
    assert result.returncode == 0, result.stderr + result.stdout
    status_one = _cli(workspace.runtime, "status")
    status_two = _cli(workspace.runtime, "status")
    assert status_one.returncode == status_two.returncode == 0
    assert json.loads(status_one.stdout) == json.loads(status_two.stdout)
    verified = _cli(workspace.runtime, "verify")
    assert verified.returncode == 0, verified.stderr + verified.stdout
    payload = json.loads(verified.stdout)
    assert payload["ok"] is True
    assert payload["persisted_samples"] == 1


def test_resume_detects_stale_and_half_written_buffer_snapshots(workspace) -> None:
    engine, _paths = workspace.create(
        partitions={**DEFAULT_PARTITIONS, "partition_count": 1}
    )
    engine.ingest_document({"events": [event(event_id="buffer-check")]})
    snapshot = engine.store.buffer_dir / "partition-0000.json"
    snapshot.write_text(
        json.dumps({"buffer_depth": 999, "buffer_digest": "stale"}),
        encoding="utf-8",
    )
    temporary = snapshot.with_suffix(".json.tmp")
    temporary.write_text("partial", encoding="utf-8")
    report = engine.resume()
    assert report["stale_buffer_snapshots"] == [
        {"partition": 0, "reason": "digest_or_depth_mismatch"}
    ]
    assert report["temporary_buffer_files_removed"]
    assert not temporary.exists()
    regenerated = json.loads(snapshot.read_text(encoding="utf-8"))
    assert regenerated["buffer_depth"] == 1

