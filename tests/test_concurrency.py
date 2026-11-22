from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from quicsim.pipeline import status_payload, verify_payload

ROOT = Path(__file__).resolve().parents[1]
BASIC = ROOT / "data" / "basic_scene.json"


def test_concurrent_resume_workers_commit_each_event_once(tmp_path: Path):
    db_path = tmp_path / "concurrent.sqlite"
    command = [
        sys.executable,
        "-m",
        "quicsim",
        "resume",
        "--scene",
        str(BASIC),
        "--db",
        str(db_path),
        "--commit-every",
        "1",
        "--max-events",
        "1",
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(4)
    ]
    results = [process.communicate(timeout=60) for process in processes]
    for process, (stdout, stderr) in zip(processes, results):
        assert process.returncode == 0, stderr
        payload = json.loads(stdout)
        assert payload["complete"] is False

    status = status_payload(db_path)
    assert status["watermark_event"] == 4
    assert status["version"] == 4

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    commits = conn.execute(
        """
        SELECT event_seq, version, COUNT(*) AS count
        FROM event_commits GROUP BY event_seq, version ORDER BY event_seq
        """
    ).fetchall()
    assert [row["event_seq"] for row in commits] == [1, 2, 3, 4]
    assert all(row["count"] == 1 for row in commits)
    assert len({row["version"] for row in commits}) == 4
    conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "quicsim",
            "resume",
            "--scene",
            str(BASIC),
            "--db",
            str(db_path),
            "--commit-every",
            "5",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    verification = verify_payload(db_path, scene_path=BASIC)
    assert verification["ok"] is True

