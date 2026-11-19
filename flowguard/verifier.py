"""Independent consistency and deterministic replay verification."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .engine import Adjudicator, _event_from_row
from .errors import ErrorCode, FlowGuardError
from .models import parse_policy, parse_sample, parse_topology
from .store import SCHEMA_VERSION, Store, meta_get
from .util import canonical_json


class Verifier:
    def __init__(self, db_path: str | Path):
        self.store = Store(db_path)
        self.adjudicator = Adjudicator(db_path)

    def verify(
        self,
        *,
        expected_digests: dict[str, str] | None = None,
        expected_decision_digest: str | None = None,
        replay: bool = True,
    ) -> dict[str, Any]:
        if not self.store.is_loaded():
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "database has not been loaded",
                {"database": str(self.store.path)},
            )
        conn = self.store.connect()
        try:
            if meta_get(conn, "topology_digest") is None:
                raise FlowGuardError(
                    ErrorCode.NOT_FOUND,
                    "database has not been loaded",
                    {"database": str(self.store.path)},
                )
            schema_version = int(meta_get(conn, "schema_version") or 0)
            if schema_version != SCHEMA_VERSION:
                raise FlowGuardError(
                    ErrorCode.STATE_INCONSISTENT,
                    "schema version mismatch",
                    {
                        "expected": SCHEMA_VERSION,
                        "actual": schema_version,
                    },
                )
            digests = {
                "topology": meta_get(conn, "topology_digest"),
                "events": meta_get(conn, "events_digest"),
                "policy": meta_get(conn, "policy_digest"),
            }
            if expected_digests is not None and digests != expected_digests:
                raise FlowGuardError(
                    ErrorCode.SAMPLE_DIGEST_MISMATCH,
                    "sample digest mismatch",
                    {
                        "expected": expected_digests,
                        "actual": digests,
                    },
                )
            actual_decision_digest = self.store.decision_digest()
            if (
                expected_decision_digest is not None
                and actual_decision_digest != expected_decision_digest
            ):
                raise FlowGuardError(
                    ErrorCode.SAMPLE_DIGEST_MISMATCH,
                    "decision digest mismatch",
                    {
                        "expected": expected_decision_digest,
                        "actual": actual_decision_digest,
                    },
                )

            integrity_rows = [
                row[0] for row in conn.execute("PRAGMA integrity_check").fetchall()
            ]
            integrity_ok = integrity_rows == ["ok"]
            checks = self._structural_checks(conn)
            checks["sqlite_integrity"] = {
                "ok": integrity_ok,
                "detail": integrity_rows,
            }
            pending = conn.execute(
                """
                SELECT COUNT(*) FROM events e
                LEFT JOIN decisions d ON d.seq = e.seq
                WHERE d.seq IS NULL
                """
            ).fetchone()[0]
            replay_ok: bool | None = None
            replay_digest: str | None = None
            replay_detail: dict[str, Any]
            if replay and int(pending) == 0:
                replay_digest = self._replay(conn)
                replay_ok = replay_digest == actual_decision_digest
                replay_detail = {
                    "performed": True,
                    "ok": replay_ok,
                    "digest": replay_digest,
                }
            elif replay:
                replay_detail = {
                    "performed": False,
                    "reason": "database_has_pending_events",
                    "pending_events": int(pending),
                    "ok": True,
                }
            else:
                replay_detail = {"performed": False, "reason": "disabled", "ok": True}
            checks["deterministic_replay"] = replay_detail
            ok = integrity_ok and all(
                item.get("ok", False)
                for item in checks.values()
                if isinstance(item, dict)
            )
            if not ok:
                failed = [
                    name
                    for name, item in checks.items()
                    if isinstance(item, dict) and not item.get("ok", False)
                ]
                raise FlowGuardError(
                    ErrorCode.STATE_INCONSISTENT,
                    "verification failed",
                    {"failed_checks": failed, "checks": checks},
                )
            return {
                "ok": True,
                "command": "verify",
                "schema_version": schema_version,
                "digests": digests,
                "decision_digest": actual_decision_digest,
                "replay_decision_digest": replay_digest,
                "checks": checks,
            }
        finally:
            conn.close()

    def _structural_checks(
        self,
        conn: sqlite3.Connection,
    ) -> dict[str, dict[str, Any]]:
        duplicate_events = int(
            conn.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT seq) FROM events"
            ).fetchone()[0]
        )
        duplicate_window_events = int(
            conn.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT seq) FROM window_events"
            ).fetchone()[0]
        )
        processed_without_decision = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM events e
                LEFT JOIN decisions d ON d.seq = e.seq
                WHERE e.status = 'processed' AND d.seq IS NULL
                """
            ).fetchone()[0]
        )
        decision_without_event = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decisions d
                LEFT JOIN events e ON e.seq = d.seq
                WHERE e.seq IS NULL
                """
            ).fetchone()[0]
        )
        staged_without_job = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM events e
                LEFT JOIN pending_jobs j ON j.seq = e.seq
                WHERE e.status = 'staged' AND j.seq IS NULL
                """
            ).fetchone()[0]
        )
        pending_marked_committed = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM pending_jobs j
                LEFT JOIN decisions d ON d.seq = j.seq
                WHERE j.status = 'pending' AND d.seq IS NOT NULL
                """
            ).fetchone()[0]
        )
        missing_window_snapshot = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM decisions d
                LEFT JOIN window_snapshots w
                  ON w.service = d.service
                 AND w.window_id = d.window_id
                 AND w.version = d.window_version
                WHERE d.window_version != '' AND w.version IS NULL
                """
            ).fetchone()[0]
        )
        exact_over_limit = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM window_snapshots
                WHERE stats_mode = 'exact'
                  AND event_count > json_extract(payload_json, '$.retention_limit')
                """
            ).fetchone()[0]
        )
        token_overflow = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM service_state
                WHERE token_units < 0
                """
            ).fetchone()[0]
        )
        dangling_event_window = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM event_windows ew
                LEFT JOIN decisions d ON d.seq = ew.seq
                WHERE d.seq IS NULL
                """
            ).fetchone()[0]
        )
        checks = {
            "duplicate_event_sequences": {
                "ok": duplicate_events == 0,
                "count": duplicate_events,
            },
            "duplicate_counted_events": {
                "ok": duplicate_window_events == 0,
                "count": duplicate_window_events,
            },
            "processed_decisions_present": {
                "ok": processed_without_decision == 0,
                "count": processed_without_decision,
            },
            "decisions_reference_events": {
                "ok": decision_without_event == 0,
                "count": decision_without_event,
            },
            "staged_jobs_present": {
                "ok": staged_without_job == 0,
                "count": staged_without_job,
            },
            "pending_jobs_not_committed": {
                "ok": pending_marked_committed == 0,
                "count": pending_marked_committed,
            },
            "window_snapshots_present": {
                "ok": missing_window_snapshot == 0,
                "count": missing_window_snapshot,
            },
            "exact_retention_contract": {
                "ok": exact_over_limit == 0,
                "count": exact_over_limit,
            },
            "token_balances_non_negative": {
                "ok": token_overflow == 0,
                "count": token_overflow,
            },
            "event_windows_link_decisions": {
                "ok": dangling_event_window == 0,
                "count": dangling_event_window,
            },
        }
        return checks

    def _replay(self, conn: sqlite3.Connection) -> str:
        topology_raw = json.loads(meta_get(conn, "topology_json") or "{}")
        policy_raw = json.loads(meta_get(conn, "policy_json") or "{}")
        event_rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        event_raw = [_event_from_row(row).normalized() for row in event_rows]
        topology = parse_topology(topology_raw)
        parse_policy(policy_raw, topology)
        sample = parse_sample(
            topology_raw,
            {"events": event_raw},
            policy_raw,
        )
        with tempfile.TemporaryDirectory(
            prefix=".flowguard-verify-",
            dir=str(Path.cwd()),
        ) as tmpdir:
            replay_db = Path(tmpdir) / "replay.db"
            replay = Adjudicator(replay_db)
            replay.load_sample(sample, reset=True)
            report = replay.run()
            if report["errors"]:
                raise FlowGuardError(
                    ErrorCode.STATE_INCONSISTENT,
                    "deterministic replay produced event errors",
                    {"errors": report["errors"]},
                )
            return report["decision_digest"]


def compare_sample_digests(
    topology_path: str | Path,
    events_path: str | Path,
    policy_path: str | Path,
) -> dict[str, str]:
    from .models import load_json_file

    sample = parse_sample(
        load_json_file(topology_path),
        load_json_file(events_path),
        load_json_file(policy_path),
    )
    return sample.digests()
