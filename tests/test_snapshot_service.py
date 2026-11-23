from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vocalforge.errors import AppError
from vocalforge.service import LabService
from vocalforge.snapshot import SECTION_NAMES, SnapshotStore


class SnapshotServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="vf-service-"))
        cls.service = LabService(cls.root)

    def test_initial_snapshot_has_three_checksummed_sections(self) -> None:
        current = self.service.snapshot_state()["current"]
        self.assertEqual(current["version"], 1)
        self.assertEqual(set(current["sections"]), set(SECTION_NAMES))
        self.assertTrue(self.service.snapshot_state()["verification"]["ok"])

    def test_registration_advances_snapshot_forward(self) -> None:
        before = self.service.snapshots.current.version
        result = self.service.append_registration(2)
        self.assertTrue(result["ok"])
        self.assertGreater(result["snapshot"]["version"], before)
        self.assertTrue(self.service.snapshots.verify()["ok"])

    def test_rollback_is_rejected(self) -> None:
        result = self.service.attempt_snapshot_rollback(1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "E_SNAPSHOT_ROLLBACK")

    def test_all_injection_stages_return_expected_codes(self) -> None:
        expected = {
            "sample_rate": "E_SAMPLE_RATE",
            "seed_shape": "E_SEED_SHAPE",
            "ref_missing": "E_REF_MISSING",
            "duplicate_utterance": "E_DUPLICATE_UTTERANCE",
            "phoneme_out_of_range": "E_PHONEME_OUT_OF_RANGE",
        }
        for stage, code in expected.items():
            result = self.service.inject_error(stage)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(result["injection_stage"], stage)
            self.assertIn("category", result["degradation"])
            self.assertTrue(result["active_state_preserved"])

    def test_consistency_self_check_passes(self) -> None:
        result = self.service.run_consistency()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["first_difference"], None)
        self.assertTrue(result["checks"]["threshold_stability"]["stable"])
        self.assertTrue(result["checks"]["topk_stability"]["stable"])

    def test_self_check_reports_first_difference(self) -> None:
        service = LabService(tempfile.mkdtemp(prefix="vf-consistency-"))
        service._spectra["fixed"]["levels"]["feature_scaling"]["intervals"][0][
            "start_frame"
        ] = 9999
        result = service.run_consistency()
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "E_INCONSISTENT")
        self.assertIsNotNone(result["first_difference"])

    def test_stale_snapshot_commit_is_rejected(self) -> None:
        store = SnapshotStore(
            Path(tempfile.mkdtemp(prefix="vf-snapshot-store-")),
            dataset_digest="d",
        )
        payloads = {name: {"value": name} for name in SECTION_NAMES}
        first = store.bootstrap(payloads)
        with self.assertRaises(AppError) as context:
            store.commit(payloads, expected_version=first.version + 5)
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")


if __name__ == "__main__":
    unittest.main()
