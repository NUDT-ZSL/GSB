from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from lbhealth.config import parse_config
from lbhealth.engine import HealthController, HealthEngine
from lbhealth.errors import LedgerError
from lbhealth.ledger import Ledger
from lbhealth.models import MemberState, RuntimeState
from lbhealth.recovery import recover_session
from lbhealth.snapshot import SnapshotService

from .helpers import PlanProber, base_raw, make_config


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ledger.sqlite"
        self.config = make_config()
        self.ledger = Ledger(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_complete_round_persists_all_required_columns(self) -> None:
        engine = HealthEngine(self.config)
        prober = PlanProber(
            self.config,
            {
                1: {"alpha": True, "bravo": False},
                2: {"alpha": True, "bravo": False},
            },
        )
        controller = HealthController(self.config, self.ledger, engine, prober)
        controller.run_round()
        controller.run_round()
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            rounds = conn.execute("SELECT * FROM round_attempts ORDER BY round_no").fetchall()
            probes = conn.execute("SELECT * FROM probes ORDER BY round_no, member_id").fetchall()
            transitions = conn.execute("SELECT * FROM transitions ORDER BY round_no").fetchall()
        self.assertEqual([row["status"] for row in rounds], ["complete", "complete"])
        self.assertEqual(len(probes), 4)
        self.assertEqual(len(transitions), 2)
        self.assertEqual(rounds[1]["config_version"], self.config.config_version)
        self.assertEqual(rounds[1]["member_digest"], self.config.member_digest)
        self.assertEqual(rounds[1]["effective_weight_sum"], 15)
        self.assertIn("alpha", rounds[1]["available_json"])
        self.assertIsNotNone(rounds[1]["completed_mono_ms"])

    def test_incomplete_round_is_unknown_and_rerun_without_inflight_recovery(self) -> None:
        attempt_id = self.ledger.begin_round(
            round_no=1,
            config=self.config,
            started_mono_ms=time.monotonic_ns() // 1_000_000,
            started_utc="test",
        )
        self.assertGreater(attempt_id, 0)
        recovered = recover_session(self.config, self.ledger)
        self.assertTrue(recovered.report.requires_rerun)
        self.assertEqual(recovered.report.recovered_round, 0)
        self.assertEqual(recovered.report.unknown_rounds[0]["round"], 1)
        self.assertFalse(recovered.report.in_flight_results_recovered)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM probes").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT status FROM round_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()[0],
                "unknown",
            )

        engine = recovered.engine
        controller = HealthController(
            self.config,
            self.ledger,
            engine,
            PlanProber(self.config, {1: {"alpha": True, "bravo": True}}),
        )
        controller.run_round(1)
        second = recover_session(self.config, self.ledger)
        self.assertEqual(second.engine.current_round, 1)
        self.assertFalse(second.report.requires_rerun)
        self.assertEqual(second.report.unknown_rounds, [])
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT round_no, attempt_no, status FROM round_attempts ORDER BY attempt_no"
            ).fetchall()
        self.assertEqual(rows, [(1, 1, "unknown"), (1, 2, "complete")])

    def test_complete_round_cannot_be_rewritten(self) -> None:
        engine = HealthEngine(self.config)
        controller = HealthController(
            self.config,
            self.ledger,
            engine,
            PlanProber(self.config, {1: {"alpha": True, "bravo": True}}),
        )
        controller.run_round(1)
        with self.assertRaises(LedgerError) as caught:
            self.ledger.begin_round(1, self.config, 0, "test")
        self.assertEqual(caught.exception.code, "E_LEDGER_ROUND")

    def test_ledger_config_mismatch_is_rejected(self) -> None:
        engine = HealthEngine(self.config)
        controller = HealthController(
            self.config,
            self.ledger,
            engine,
            PlanProber(self.config, {1: {"alpha": True, "bravo": True}}),
        )
        controller.run_round(1)
        raw = base_raw()
        raw["config_version"] = "test-v2"
        changed = parse_config(raw)
        with self.assertRaises(LedgerError) as caught:
            recover_session(changed, self.ledger)
        self.assertEqual(caught.exception.code, "E_LEDGER_CONFIG")

    def test_snapshot_mismatch_performs_whole_state_replacement(self) -> None:
        engine = HealthEngine(self.config)
        prober = PlanProber(
            self.config,
            {round_no: {"alpha": True, "bravo": True} for round_no in range(1, 5)},
        )
        controller = HealthController(self.config, self.ledger, engine, prober)
        controller.run_rounds(4)
        service = SnapshotService(self.ledger)
        snapshot = service.create(self.config, engine, label="before-change", window=3)

        raw = base_raw()
        raw["config_version"] = "test-v2"
        members = raw["pool"]["members"]
        members[0]["weight"] = 70
        members.pop(1)
        members.append(
            {
                "id": "gamma",
                "address": "127.0.0.1",
                "port": 19003,
                "protocol": "tcp",
                "weight": 10,
            }
        )
        changed = parse_config(raw)
        previous = engine.snapshot_state()
        previous.member_states["gamma"] = MemberState(
            member_id="gamma",
            state=MemberState(member_id="x").state,
            success_count=2,
            failure_count=0,
        )
        previous.effective_weights["gamma"] = 10
        previous.available_members = tuple(previous.available_members) + ("gamma",)
        restore = service.restore(snapshot.snapshot_id, changed, previous)

        self.assertEqual(restore.mode, "full_replace")
        self.assertFalse(restore.config_version_match)
        self.assertFalse(restore.member_digest_match)
        self.assertIn("gamma", restore.added_members)
        self.assertIn("bravo", restore.removed_members)
        self.assertEqual(restore.state.current_round, 4)
        self.assertEqual(restore.state.member_states["gamma"].state.value, "unknown")
        self.assertEqual(restore.state.member_states["gamma"].success_count, 0)
        self.assertEqual(restore.state.member_states["alpha"].state.value, "healthy")
        self.assertEqual(restore.state.effective_weights["gamma"], 0)
        self.assertIn("gamma", [item["member"] for item in restore.reset_counters])
        self.assertNotIn("bravo", restore.state.member_states)
        self.assertTrue(restore.note.startswith("All previous member states"))

    def test_snapshot_stores_progress_counters_and_recent_rounds(self) -> None:
        engine = HealthEngine(self.config)
        controller = HealthController(
            self.config,
            self.ledger,
            engine,
            PlanProber(
                self.config,
                {round_no: {"alpha": True, "bravo": True} for round_no in range(1, 4)},
            ),
        )
        controller.run_rounds(3)
        snapshot = SnapshotService(self.ledger).create(self.config, engine, label="full", window=2)
        self.assertEqual(snapshot.round_no, 3)
        self.assertEqual([item["round"] for item in snapshot.recent_rounds], [2, 3])
        self.assertEqual(snapshot.state.member_states["alpha"].success_count, 2)
        self.assertEqual(snapshot.state.member_states["alpha"].slow_start_index, 1)
        self.assertEqual(snapshot.replay_baseline.current_round, 1)
        self.assertEqual(len(snapshot.probe_sequence), 2)


if __name__ == "__main__":
    unittest.main()
