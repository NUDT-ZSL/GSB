from __future__ import annotations

import json
import sqlite3

from shardroute.engine import QueryEngine
from shardroute.verify import verify_state


def test_verify_detects_tampered_result_digest(state):
    QueryEngine(state, 1100).query("orders_eq")
    connection = sqlite3.connect(state.path)
    try:
        row = connection.execute(
            "SELECT result_json FROM query_results"
        ).fetchone()
        result = json.loads(row[0])
        result["rows"][0]["amount"] = 999
        connection.execute(
            "UPDATE query_results SET result_json = ?",
            (json.dumps(result, sort_keys=True, separators=(",", ":")),),
        )
        connection.commit()
    finally:
        connection.close()
    report = verify_state(state)
    assert report["ok"] is False
    assert any(issue["code"] == "E_VERIFY_RESULT_DIGEST" for issue in report["issues"])

