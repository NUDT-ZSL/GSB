from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from h2diag.service import DiagnosticService


def test_verify_reports_first_divergent_frame(
    tmp_path: Path, main_scenario: dict
) -> None:
    db_path = tmp_path / "divergence.db"
    service = DiagnosticService(db_path)
    service.initialize(main_scenario)
    service.run("demo-connection")
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT commit_order, state_json FROM state_commits
            WHERE connection_id='demo-connection' AND kind='frame' AND frame_seq=5
            """
        ).fetchone()
        state = json.loads(row[1])
        state["connection"]["send_window"] += 1
        connection.execute(
            "UPDATE state_commits SET state_json=? WHERE commit_order=?",
            (json.dumps(state, sort_keys=True, separators=(",", ":")), row[0]),
        )
        connection.commit()
    report = service.verify("demo-connection")
    assert report["ok"] is False
    assert report["first_divergence"]["frame_seq"] == 5
    assert report["first_divergence"]["path"] == "connection.send_window"
    assert (
        report["first_divergence"]["persisted"]
        != report["first_divergence"]["recomputed"]
    )
    assert report["first_divergence"]["observation"]["observation"] == "frame-state-replay"
