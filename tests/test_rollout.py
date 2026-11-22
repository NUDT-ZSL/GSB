from __future__ import annotations

import json
import unittest

from hotgray.errors import BizError, ErrorCode
from hotgray.inference import compute_inference

from .support import (
    TempWorkspace,
    new_engine,
    request_id_at_least,
    request_id_under,
    sqlite_rows,
    warmed_engine,
)

class RolloutTests(unittest.TestCase):
    def test_cannot_advance_gray_before_warmup_and_reports_not_ready(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            engine.apply_patch(at_ms=1000)
            result = engine.advance_stage(at_ms=1200, level=1)
            self.assertTrue(result["ok"])
            self.assertFalse(result["advanced"])
            self.assertEqual(result["level"], 0)
            self.assertTrue(result["not_ready"])
            types = {item["type"] for item in result["not_ready"]}
            self.assertIn("replica_state", types)
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["round"]["current_level"], 0)
            self.assertEqual(snapshot["round"]["current_ratio"], 0.0)

    def test_warmup_state_machine_and_stage_progression(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            engine.apply_patch(at_ms=1000)
            engine.start_warmup(at_ms=1000)
            snapshot = engine.snapshot()
            self.assertEqual(
                [item["state"] for item in snapshot["replicas"] if item["eligible"]],
                ["loading", "loading"],
            )
            engine.tick(at_ms=1099)
            snapshot = engine.snapshot()
            self.assertTrue(
                all(item["state"] == "loading" for item in snapshot["replicas"] if item["eligible"])
            )
            engine.tick(at_ms=1100)
            snapshot = engine.snapshot()
            self.assertTrue(
                all(item["state"] == "prewarmed" for item in snapshot["replicas"] if item["eligible"])
            )
            self.assertEqual(snapshot["round"]["status"], "ready")
            first = engine.advance_stage(at_ms=1200, level=1)
            self.assertTrue(first["advanced"])
            self.assertEqual(first["ratio"], 0.1)
            second = engine.advance_stage(at_ms=1200, level=1)
            self.assertTrue(second["reused"])
            engine.advance_stage(at_ms=1300, level=2)
            final = engine.advance_stage(at_ms=1400, level=3)
            self.assertEqual(final["status"], "released")
            snapshot = engine.snapshot()
            self.assertEqual(snapshot["round"]["current_ratio"], 1.0)
            self.assertTrue(
                all(item["serving_version"] == 2 for item in snapshot["replicas"] if item["eligible"])
            )
            at_max = engine.advance_stage(at_ms=1450)
            self.assertTrue(at_max["reused"])
            self.assertFalse(at_max["advanced"])
            with self.assertRaises(BizError) as raised:
                engine.advance_stage(at_ms=1460, level=1)
            self.assertEqual(raised.exception.code, ErrorCode.CONFLICT)

    def test_request_binding_is_immutable_across_stage_changes(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            target_request = request_id_under("target-request", 0.1)
            baseline_request = request_id_at_least("baseline-request", 0.1)
            start_target = engine.begin_request(
                replica_id="replica-a",
                request_id=target_request,
                input_payload=[1.0, 0.5, -0.25],
                at_ms=1210,
            )
            start_baseline = engine.begin_request(
                replica_id="replica-b",
                request_id=baseline_request,
                input_payload=[1.0, 0.5, -0.25],
                at_ms=1210,
            )
            self.assertEqual(start_target["version"], 2)
            self.assertEqual(start_target["lane"], "target")
            self.assertEqual(start_baseline["version"], 1)
            self.assertEqual(start_baseline["lane"], "baseline")

            binding_rows = sqlite_rows(
                engine.store.db_path,
                "SELECT request_id, binding_json FROM requests ORDER BY request_id",
            )
            original_bindings = {
                row["request_id"]: json.loads(row["binding_json"]) for row in binding_rows
            }
            engine.advance_stage(at_ms=1300, level=2)
            engine.advance_stage(at_ms=1400, level=3)

            completed_target = engine.complete_request(request_id=target_request, at_ms=1410)
            completed_baseline = engine.complete_request(request_id=baseline_request, at_ms=1420)
            expected_target = compute_inference(
                blob_dir=engine.materializer.blob_dir,
                binding=original_bindings[target_request],
                payload=[1.0, 0.5, -0.25],
            )
            expected_baseline = compute_inference(
                blob_dir=engine.materializer.blob_dir,
                binding=original_bindings[baseline_request],
                payload=[1.0, 0.5, -0.25],
            )
            self.assertEqual(completed_target["result"]["output_bits"], expected_target["output_bits"])
            self.assertEqual(completed_baseline["result"]["output_bits"], expected_baseline["output_bits"])
            self.assertEqual(completed_target["version"], 2)
            self.assertEqual(completed_baseline["version"], 1)
            repeated = engine.complete_request(request_id=target_request, at_ms=1430)
            self.assertTrue(repeated["reused"])
            self.assertEqual(
                repeated["result"]["output_bits"],
                completed_target["result"]["output_bits"],
            )

    def test_unaffected_control_replica_result_is_bitwise_stable(self) -> None:
        with TempWorkspace() as workspace:
            engine = new_engine(workspace.path)
            request_id = "control-before-update"
            engine.begin_request(
                replica_id="replica-control",
                request_id=request_id,
                input_payload=[-0.75, 0.25, 0.5],
                at_ms=0,
            )
            engine.complete_request(request_id=request_id, at_ms=0)
            before = sqlite_rows(
                engine.store.db_path,
                "SELECT result_bytes_hex, result_digest FROM requests WHERE request_id = ?",
                (request_id,),
            )[0]
            engine.execute_clock_script("normal")
            after = sqlite_rows(
                engine.store.db_path,
                "SELECT result_bytes_hex, result_digest FROM requests WHERE request_id = ?",
                (request_id,),
            )[0]
            self.assertEqual(before, after)
            snapshot = engine.snapshot()
            control = next(item for item in snapshot["replicas"] if item["replica_id"] == "replica-control")
            self.assertEqual(control["serving_version"], 1)
            self.assertEqual(control["invalidated"], [])

    def test_duplicate_request_id_is_idempotent(self) -> None:
        with TempWorkspace() as workspace:
            engine = warmed_engine(workspace.path)
            engine.advance_stage(at_ms=1200, level=1)
            request_id = request_id_under("same-request", 0.1)
            first = engine.begin_request(
                replica_id="replica-a",
                request_id=request_id,
                input_payload=[1, 2, 3],
                at_ms=1210,
            )
            second = engine.begin_request(
                replica_id="replica-a",
                request_id=request_id,
                input_payload=[1, 2, 3],
                at_ms=1220,
            )
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            self.assertEqual(
                sqlite_rows(engine.store.db_path, "SELECT COUNT(*) AS count FROM requests")[0]["count"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
