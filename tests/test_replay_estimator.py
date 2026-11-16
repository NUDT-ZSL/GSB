from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from replication_lab.estimator import (
    EstimatorParameters,
    ReplayParameters,
    estimate_delays,
)
from replication_lab.errors import StateError
from replication_lab.replay import build_replay_plan
from replication_lab.segment import LogRecord
from replication_lab.segment import decode_record_payload
from replication_lab.service import LabSession

try:
    from tests.helpers import write_legacy_segments
except ModuleNotFoundError:  # support `unittest discover -s tests` without -t
    from helpers import write_legacy_segments


class EstimatorTests(unittest.TestCase):
    def test_fixed_point_mean_p95_max_and_bounds(self) -> None:
        samples = [
            {"delay_ms": value}
            for value in [10, 20, 30, 40, 100]
        ]
        result = estimate_delays(
            samples,
            parameters=EstimatorParameters(),
            replay_parameters=ReplayParameters(10, 2, 1000),
        )
        self.assertEqual(result["mean_fixed"], 40_000)
        self.assertEqual(result["p95_ms"], 100)
        self.assertEqual(result["max_delay_ms"], 100)
        self.assertEqual(result["schedule_error_upper_bound_ms"], 3)
        self.assertEqual(result["estimate_status"], "COMPLETE")
        self.assertEqual(result["confidence"], "HIGH")
        self.assertTrue(result["error_bound_steps"])

    def test_unknown_delays_are_retained_and_degrade_status(self) -> None:
        samples = [{"delay_ms": value} for value in [10, 20, 30, 40, 50]]
        samples.append({"delay_ms": None})
        result = estimate_delays(samples, replay_parameters=ReplayParameters(10, 2, 1000))
        self.assertEqual(result["sample_count"], 6)
        self.assertEqual(result["known_delay_count"], 5)
        self.assertEqual(result["unknown_delay_count"], 1)
        self.assertEqual(result["estimate_status"], "DEGRADED_PARTIAL")
        self.assertEqual(result["confidence"], "LOW")
        self.assertIn("不填补", result["error_bound_steps"][4])

    def test_insufficient_and_no_known_samples(self) -> None:
        low = estimate_delays([{"delay_ms": 10}, {"delay_ms": None}])
        self.assertEqual(low["estimate_status"], "INSUFFICIENT")
        none = estimate_delays([{"delay_ms": None}])
        self.assertEqual(none["known_delay_count"], 0)
        self.assertIsNone(none["mean_fixed"])
        self.assertEqual(none["confidence"], "NONE")

    def test_replay_plan_is_deterministic_and_has_unknown_v1_sample(self) -> None:
        records = [
            LogRecord(3, 1, 1001, 1000, 10),
            LogRecord(1, 2, 1002, None, None),
            LogRecord(2, 3, 1003, 1200, 20),
        ]
        plan1 = build_replay_plan(records, replay_rate=10, concurrency=2)
        plan2 = build_replay_plan(records, replay_rate=10, concurrency=2)
        self.assertEqual(plan1.samples, plan2.samples)
        self.assertEqual(plan1.parameters.batch_duration_ms, 200)
        self.assertEqual(plan1.parameters.clock_origin_ms, 1200)
        self.assertEqual(plan1.samples[0]["completion_ts_ms"], 1400)
        self.assertIsNone(plan1.samples[1]["delay_ms"])
        self.assertEqual(plan1.samples[1]["status"], "DELAY_UNKNOWN")


class CrashRecoveryTests(unittest.TestCase):
    def test_master_operation_payloads_are_persisted_in_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with LabSession.open(Path(temp)) as session:
                session.write_operations(
                    [
                        {
                            "commit_seq": 1,
                            "txn_id": 1001,
                            "commit_offset_ms": 10,
                            "txn_size_bytes": 128,
                        }
                    ],
                    records_per_segment=1,
                )
                payload_rows = session.store.operation_payloads()
                self.assertEqual(len(payload_rows), 1)
                record = decode_record_payload(payload_rows[0]["payload"])
                self.assertEqual(record.commit_seq, 1)
                self.assertEqual(record.commit_ts_ms, 1_000_010)
                self.assertEqual(session.store.operation_rows()[0]["txn_id"], 1001)

    def test_partial_progress_survives_record_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_legacy_segments(root / "logs")
            with LabSession.open(root, auto_upgrade=False) as session:
                self.assertEqual(session.version_set(), [1, 2, 3])
                partial = session.replay(
                    replay_rate=10,
                    concurrency=2,
                    reset=True,
                    max_new_batches=1,
                )
                self.assertEqual(partial["sample_count"], 2)
            with LabSession.open(root, auto_upgrade=True) as upgraded:
                self.assertEqual(upgraded.version_set(), [3])
                resumed = upgraded.replay(
                    replay_rate=10, concurrency=2, reset=False
                )
                self.assertEqual(resumed["sample_count"], 7)
                self.assertEqual(
                    [sample["commit_version"] for sample in resumed["samples"]],
                    [1, 1, 2, 2, 2, 3, 3],
                )
                self.assertEqual(resumed["estimate"]["unknown_delay_count"], 2)

    def test_forced_batch_is_unknown_and_resume_does_not_duplicate_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with LabSession.open(root) as session:
                session.write_operations(
                    [
                        {
                            "commit_seq": seq,
                            "txn_id": 1000 + seq,
                            "commit_offset_ms": seq * 100,
                            "txn_size_bytes": seq * 64,
                        }
                        for seq in range(1, 9)
                    ],
                    records_per_segment=4,
                )

                def crash(batch: object) -> None:
                    if getattr(batch, "batch_id") == 1:
                        raise RuntimeError("simulated kill")

                with self.assertRaises(RuntimeError):
                    session.replay(
                        replay_rate=10,
                        concurrency=2,
                        reset=True,
                        crash_hook=crash,
                    )

            with LabSession.open(root) as session:
                recovery = session.recover()
                self.assertEqual(recovery["recovered_last_commit_seq"], 2)
                self.assertEqual(recovery["unknown_batch_count"], 1)
                self.assertEqual(recovery["rebuilt_sample_count"], 2)
                self.assertFalse(recovery["claim_exit_report"])
                repeated_recovery = session.recover()
                self.assertEqual(repeated_recovery["unknown_batch_count"], 1)
                self.assertEqual(repeated_recovery["newly_marked_unknown_batch_ids"], [])
                resumed = session.replay(replay_rate=10, concurrency=2, reset=False)
                self.assertEqual(resumed["sample_count"], 8)
                self.assertEqual(resumed["unknown_batch_count"], 0)
                seqs = [sample["commit_seq"] for sample in resumed["samples"]]
                self.assertEqual(seqs, list(range(1, 9)))
                again = session.replay(replay_rate=10, concurrency=2, reset=False)
                self.assertEqual(again["sample_count"], 8)
                self.assertEqual(again["newly_completed_batch_ids"], [])

    def test_resume_parameter_mismatch_requires_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with LabSession.open(Path(temp)) as session:
                session.write_operations(
                    [
                        {
                            "commit_seq": seq,
                            "txn_id": 1000 + seq,
                            "commit_offset_ms": seq * 100,
                            "txn_size_bytes": 10,
                        }
                        for seq in range(1, 5)
                    ],
                    records_per_segment=2,
                )
                session.replay(replay_rate=10, concurrency=2, reset=True)
                with self.assertRaises(StateError) as caught:
                    session.replay(replay_rate=11, concurrency=2, reset=False)
                self.assertEqual(caught.exception.code, "E_STATE")


if __name__ == "__main__":
    unittest.main()
