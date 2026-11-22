#!/usr/bin/env python3
"""Run the deterministic loopback demo end to end."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mesh_drift.canon import read_json
from mesh_drift.mock_server import MockCluster
from mesh_drift.runner import reconcile_service
from mesh_drift.store import Database
from mesh_drift.validation import validate_expected, validate_policy
from mesh_drift.verify import verify_database


def main() -> int:
    expected = validate_expected(read_json(ROOT / "samples" / "expected.json"))
    policy = validate_policy(read_json(ROOT / "samples" / "policy.json"))
    scenario = read_json(ROOT / "samples" / "anomalies.json")
    with tempfile.TemporaryDirectory(prefix="mesh-drift-demo-") as temp_dir:
        database = Database(Path(temp_dir) / "ledger.db")
        rounds = []
        with MockCluster(expected, scenario):
            for clock in (1000, 2000, 6000, 9000):
                for service_name in ("checkout", "orders"):
                    summary = reconcile_service(
                        database,
                        expected=expected,
                        policy=policy,
                        service_name=service_name,
                        logical_clock_ms=clock,
                    )
                    rounds.append(
                        {
                            "round_id": summary["round_id"],
                            "service": summary["service"],
                            "clock_ms": summary["logical_clock_ms"],
                            "drift_types": summary["drift_type_stats"],
                            "decisions": summary["decision_stats"],
                        }
                    )
        verification = verify_database(database)
        ledger = [
            {
                "entry_id": entry["entry_id"],
                "instance_id": entry["instance_id"],
                "drift_type": entry["drift_type"],
                "field_path": entry["field_path"],
                "status": entry["status"],
                "verdict": entry["verdict"],
                "occurrences": entry["occurrences"],
                "first_seen_clock_ms": entry["first_seen_clock_ms"],
                "last_seen_clock_ms": entry["last_seen_clock_ms"],
                "previous_entry_id": entry["previous_entry_id"],
            }
            for entry in database.list_ledger_entries()
        ]
        print(
            json.dumps(
                {
                    "ok": verification["ok"],
                    "rounds": rounds,
                    "ledger": ledger,
                    "verification": {
                        "ok": verification["ok"],
                        "round_count": verification["round_count"],
                        "entry_count": verification["entry_count"],
                        "database_digest": verification["database_digest"],
                        "errors": verification["errors"],
                    },
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if verification["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
