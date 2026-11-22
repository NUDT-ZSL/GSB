from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .conftest import REPO_ROOT, SAMPLES, read_cli_json, run_cli


def _bootstrap(tmp_path: Path) -> Path:
    root = tmp_path / "store"
    result = run_cli(
        root,
        "load",
        "--volume",
        str(SAMPLES / "volume.json"),
        "--snapshots",
        str(SAMPLES / "snapshots.json"),
        "--rollbacks",
        str(SAMPLES / "rollbacks.json"),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return root


def _spawn(root: Path, *args: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.pop("BLOCKSTORE_CRASH_POINT", None)
    command = [
        sys.executable,
        "-m",
        "blockstore",
        "--root",
        str(root),
        "--compact",
        *args,
    ]
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _finish(process: subprocess.Popen[str]) -> tuple[int, dict]:
    stdout, stderr = process.communicate(timeout=30)
    assert stdout, stderr
    return process.returncode, json.loads(stdout)


def test_concurrent_identical_rollback_commits_once(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    command = (
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
    )
    processes = [_spawn(root, *command) for _ in range(4)]
    results = [_finish(process) for process in processes]
    assert all(code == 0 for code, _ in results)
    versions = {payload["view_version"] for _, payload in results}
    assert versions == {7}
    assert sum(1 for _, payload in results if payload["idempotent"]) == 3
    status = read_cli_json(run_cli(root, "status"))
    assert status["view_version"] == 7
    assert status["pending_operations"] == {
        "snapshots": 0,
        "rollbacks": 0,
        "gc": 0,
    }
    assert status["anchor_snapshot_id"] == "snap-a2"
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]


def test_concurrent_equal_clock_rollbacks_have_one_winner(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    common = (
        "rollback",
        "--target",
        "snap-a2",
        "--clock",
        "10000",
    )
    first = _spawn(root, *common, "--request-id", "rb-a")
    second = _spawn(root, *common, "--request-id", "rb-b")
    results = [_finish(first), _finish(second)]
    codes = sorted(code for code, _ in results)
    assert codes == [0, 2]
    loser = next(payload for code, payload in results if code == 2)
    assert loser["error"]["code"] in {
        "ROLLBACK_CONFLICT",
        "ROLLBACK_STALE",
        "ROLLBACK_TARGET_ORPHANED",
    }
    status = read_cli_json(run_cli(root, "status"))
    assert status["view_version"] == 7
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]


def test_concurrent_snapshot_same_seq_commits_once(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    snapshot_a = tmp_path / "snapshot-a.json"
    snapshot_b = tmp_path / "snapshot-b.json"
    base = {
        "seq": 9,
        "volume_id": "vol-demo",
        "parent_snapshot_id": "snap-a2",
        "created_clock": 9000,
        "covered_start": 0,
        "covered_end": 0,
        "blocks": [
            {
                "block_no": 0,
                "data": "RACEBLK1",
                "encoding": "utf8",
                "sha256": "dddf9ff7fd636da4725868b1b6ff159159b52db1a3dcc22367ca82b208583bf8",
            }
        ],
    }
    doc_a = dict(base, snapshot_id="snap-race-a")
    doc_b = dict(base, snapshot_id="snap-race-b")
    snapshot_a.write_text(json.dumps(doc_a), encoding="utf-8")
    snapshot_b.write_text(json.dumps(doc_b), encoding="utf-8")
    processes = [
        _spawn(root, "snapshot", "--file", str(snapshot_a)),
        _spawn(root, "snapshot", "--file", str(snapshot_b)),
    ]
    results = [_finish(process) for process in processes]
    codes = sorted(code for code, _ in results)
    assert codes == [0, 2]
    loser = next(payload for code, payload in results if code == 2)
    assert loser["error"]["code"] == "SNAPSHOT_CONFLICT"
    rows = read_cli_json(run_cli(root, "status"))["snapshots"]
    assert sum(1 for item in rows if item["seq"] == 9) == 1
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]


def test_concurrent_gc_returns_same_committed_report(tmp_path: Path) -> None:
    root = _bootstrap(tmp_path)
    rollback = run_cli(
        root,
        "rollback",
        "--file",
        str(SAMPLES / "rollbacks.json"),
        "--request-id",
        "rb-main",
    )
    assert rollback.returncode == 0
    processes = [_spawn(root, "gc") for _ in range(3)]
    results = [_finish(process) for process in processes]
    assert all(code == 0 for code, _ in results)
    reports = [payload for _, payload in results]
    assert all(report["deleted_count"] == 5 for report in reports)
    assert all(report["actions"] == reports[0]["actions"] for report in reports)
    assert sum(1 for report in reports if not report["idempotent"]) == 1
    verified = read_cli_json(run_cli(root, "verify"))
    assert verified["ok"]
