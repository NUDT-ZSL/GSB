from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lbhealth.config import parse_config
from lbhealth.engine import HealthController, HealthEngine
from lbhealth.ledger import Ledger
from lbhealth.replay import ReplayVerifier
from lbhealth.snapshot import SnapshotService

from .helpers import PlanProber, SequenceProber, base_raw


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        raw = base_raw()
        raw["pool"]["success_threshold"] = 1
        raw["pool"]["failure_threshold"] = 1
        raw["pool"]["slow_start_weight_table"] = [50, 100]
        self.config = parse_config(raw)
        self.ledger = Ledger(Path(self.temp.name) / "ledger.sqlite")
        self.plan = {
            1: {"alpha": True, "bravo": False},
            2: {"alpha": True, "bravo": True},
            3: {"alpha": False, "bravo": True},
            4: {"alpha": False, "bravo": True},
        }
        engine = HealthEngine(self.config)
        controller = HealthController(
            self.config, self.ledger, engine, PlanProber(self.config, self.plan)
        )
        controller.run_rounds(4)
        self.snapshot = SnapshotService(self.ledger).create(
            self.config, engine, label="replay", window=4
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_positive_replay_matches_all_rounds(self) -> None:
        report = ReplayVerifier(
            self.config,
            prober=SequenceProber(self.config, [self.plan[i] for i in range(1, 5)]),
        ).verify(self.snapshot)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.rounds_checked, [1, 2, 3, 4])

    def test_probe_difference_locates_first_round_member_and_field(self) -> None:
        changed = copy.deepcopy([self.plan[i] for i in range(1, 5)])
        changed[1]["bravo"] = False
        report = ReplayVerifier(
            self.config, prober=SequenceProber(self.config, changed)
        ).verify(self.snapshot)
        self.assertEqual(report.code, "E_REPLAY")
        self.assertEqual(report.round_no, 2)
        self.assertEqual(report.member_id, "bravo")
        self.assertEqual(report.field, "probe.ok")
        self.assertIn("actual_probe", report.evidence)

    def test_weight_difference_locates_exact_member_field(self) -> None:
        self.snapshot.recent_rounds[2]["effective_weights"]["alpha"] += 1
        report = ReplayVerifier(
            self.config,
            prober=SequenceProber(self.config, [self.plan[i] for i in range(1, 5)]),
        ).verify(self.snapshot)
        self.assertEqual(report.code, "E_REPLAY")
        self.assertEqual(report.round_no, 3)
        self.assertEqual(report.member_id, "alpha")
        self.assertEqual(report.field, "effective_weights.alpha")

    def test_transition_difference_locates_field(self) -> None:
        self.snapshot.recent_rounds[0]["transitions"][0]["to"] = "unhealthy"
        report = ReplayVerifier(
            self.config,
            prober=SequenceProber(self.config, [self.plan[i] for i in range(1, 5)]),
        ).verify(self.snapshot)
        self.assertEqual(report.code, "E_REPLAY")
        self.assertEqual(report.round_no, 1)
        self.assertEqual(report.member_id, "alpha")
        self.assertEqual(report.field, "transition.to")

    def test_config_and_behavior_mismatch_are_explicit(self) -> None:
        raw = base_raw()
        raw["config_version"] = "different"
        changed_config = parse_config(raw)
        report = ReplayVerifier(
            changed_config,
            prober=SequenceProber(changed_config, [self.plan[i] for i in range(1, 5)]),
        ).verify(self.snapshot)
        self.assertEqual(report.code, "E_REPLAY")
        self.assertEqual(report.field, "config_version")
        self.assertIn("configuration version differs", report.evidence["basis"])

        self.snapshot.replay_context = {"behavior_manifest": "recorded"}
        behavior_report = ReplayVerifier(
            self.config,
            prober=SequenceProber(self.config, [self.plan[i] for i in range(1, 5)]),
        ).verify(self.snapshot, behavior_manifest="current")
        self.assertEqual(behavior_report.code, "E_REPLAY")
        self.assertEqual(behavior_report.field, "behavior_manifest")
        self.assertIn("behavior injection differs", behavior_report.evidence["basis"])


if __name__ == "__main__":
    unittest.main()
