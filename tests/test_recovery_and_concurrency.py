from __future__ import annotations

import json
import multiprocessing
import sqlite3
import subprocess
import sys
from pathlib import Path

from alertlab.service import AlertLabService
from alertlab.util import canonical_json

from conftest import ROOT


PYTHON = sys.executable


def run_cli(database: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            PYTHON,
            "-m",
            "alertlab.cli",
            "--workspace",
            str(ROOT),
            "--database",
            str(database),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )


def test_cluster_commit_crash_recovers_without_duplicate_suppression(
    tmp_path: Path,
) -> None:
    baseline_db = tmp_path / "baseline.sqlite3"
    baseline = run_cli(baseline_db, "load")
    assert baseline.returncode == 0, baseline.stderr
    baseline_service = AlertLabService(ROOT, database_path=baseline_db)
    expected = baseline_service.status()

    crash_db = tmp_path / "crash.sqlite3"
    crashed = run_cli(crash_db, "load", "--crash-at", "cluster_after_journal:2")
    assert crashed.returncode == 73, crashed.stderr

    recovered_service = AlertLabService(ROOT, database_path=crash_db)
    recovered = recovered_service.status()
    assert recovered == expected
    assert recovered_service.verify()["ok"] is True
    connection = sqlite3.connect(crash_db)
    try:
        phases = {
            row[0]
            for row in connection.execute(
                "SELECT phase FROM commit_journal WHERE object_type = 'cluster'"
            )
        }
    finally:
        connection.close()
    assert phases == {"committed"}


def test_patch_crash_recovers_to_identical_status(tmp_path: Path) -> None:
    baseline_db = tmp_path / "patch-baseline.sqlite3"
    assert run_cli(baseline_db, "load").returncode == 0
    baseline_patch = run_cli(
        baseline_db, "patch", "samples/patches/dependency_patch.json"
    )
    assert baseline_patch.returncode == 0, baseline_patch.stderr
    baseline_service = AlertLabService(ROOT, database_path=baseline_db)
    expected = baseline_service.status()

    crash_db = tmp_path / "patch-crash.sqlite3"
    assert run_cli(crash_db, "load").returncode == 0
    crashed = run_cli(
        crash_db,
        "patch",
        "samples/patches/dependency_patch.json",
        "--crash-at",
        "patch_before_commit",
    )
    assert crashed.returncode == 73, crashed.stderr
    recovered_service = AlertLabService(ROOT, database_path=crash_db)
    assert recovered_service.status() == expected
    assert recovered_service.verify()["ok"] is True


def _concurrent_load(database: str, queue) -> None:
    try:
        service = AlertLabService(ROOT, database_path=database)
        service.load()
        status = service.status()
        queue.put(
            (
                "ok",
                status["rule_view"]["view_id"],
                status["result_revision"],
                status["summary"]["cluster_count"],
                status["summary"]["event_count"],
            )
        )
    except Exception as exc:  # pragma: no cover - surfaced through queue
        queue.put(("error", type(exc).__name__, str(exc)))


def _concurrent_patch(database: str, queue) -> None:
    try:
        service = AlertLabService(ROOT, database_path=database)
        patch = json.loads(
            (ROOT / "samples" / "patches" / "dependency_patch.json").read_text(
                encoding="utf-8"
            )
        )
        result = service.patch(patch)
        queue.put(
            (
                "ok",
                result["rule_view"]["view_id"],
                service.status()["result_revision"],
            )
        )
    except Exception as exc:  # pragma: no cover
        queue.put(("error", type(exc).__name__, str(exc)))


def test_concurrent_load_and_patch_are_single_commit(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    database = str(tmp_path / "concurrent.sqlite3")
    queue = context.Queue()
    workers = [
        context.Process(target=_concurrent_load, args=(database, queue))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    results = [queue.get(timeout=10) for _ in workers]
    assert all(result[0] == "ok" for result in results), results
    assert {result[1:] for result in results} == {(1, 1, 6, 11)}

    queue = context.Queue()
    workers = [
        context.Process(target=_concurrent_patch, args=(database, queue))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    results = [queue.get(timeout=10) for _ in workers]
    assert all(result[0] == "ok" for result in results), results
    assert {result[1:] for result in results} == {(2, 2)}

    service = AlertLabService(ROOT, database_path=database)
    connection = sqlite3.connect(database)
    try:
        patch_count = connection.execute("SELECT COUNT(*) FROM patches").fetchone()[0]
        cluster_count = connection.execute(
            "SELECT COUNT(*) FROM clusters WHERE result_revision = ?",
            (service.status()["result_revision"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert patch_count == 1
    assert cluster_count == 6
    assert service.verify()["ok"] is True
