from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latentflow.errors import BusinessError, ErrorCode
from latentflow.imaging import make_reference_frame, save_ppm
from latentflow.service import PreviewService


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "samples").mkdir(parents=True)
        save_ppm(
            self.root / "samples" / "reference.ppm",
            make_reference_frame(48, 32),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, **overrides: object) -> dict[str, object]:
        config: dict[str, object] = {
            "prompt": "service deterministic test",
            "seed": 2026,
            "width": 48,
            "height": 32,
            "frames": 6,
            "steps": 3,
            "block_size": 2,
            "latency_budget_ms": 1000.0,
            "anchor_path": "samples/reference.ppm",
            "anchor_strength": 0.25,
            "max_concurrent_decode_tasks": 2,
        }
        config.update(overrides)
        return {"config": config, "compare_block_size": 3}

    def demo_interactive(self, service: PreviewService, payload: dict[str, object]) -> dict:
        start = service.create_job(payload)
        job_id = start["job"]["job_id"]
        chunks = []
        for index in range(start["job"]["plan"]["num_blocks"]):
            chunks.append(service.decode_chunk(job_id, index))
        completion = service.complete_job(
            job_id, int(payload.get("compare_block_size", 3))
        )
        return {"start": start, "chunks": chunks, "completion": completion}

    def test_end_to_end_anchor_chunks_and_alternate_decode(self) -> None:
        service = PreviewService(self.root)
        result = self.demo_interactive(service, self.payload())
        completion = result["completion"]

        self.assertTrue(completion["consistency"]["equal"])
        self.assertTrue(completion["alternative"]["consistency"]["equal"])
        self.assertTrue(completion["anchor_validation"]["equal"])
        self.assertEqual(
            completion["consistency"]["sha256"],
            completion["whole"]["sha256"],
        )
        self.assertGreater(completion["job"]["first_block_actual_ms"], 0.0)
        self.assertGreater(
            completion["job"]["plan"]["estimate_basis"]["measured_ms_per_frame_stage"],
            0.0,
        )
        versions = completion["snapshot_versions"]
        self.assertGreaterEqual(versions["decode_params"]["version"], 2)
        self.assertGreaterEqual(versions["chunk_plan"]["version"], 1)
        self.assertGreater(versions["ready_blocks"]["version"], 1)

    def test_stage_busy_rejects_second_plan_without_waiting(self) -> None:
        service = PreviewService(self.root)
        first = service.create_job(self.payload())
        with self.assertRaises(BusinessError) as captured:
            service.create_job(self.payload(seed=2027))
        self.assertEqual(captured.exception.code, ErrorCode.E_STAGE_BUSY)
        self.assertEqual(
            captured.exception.details["policy"], "non_blocking_reject"
        )

        job_id = first["job"]["job_id"]
        for index in range(first["job"]["plan"]["num_blocks"]):
            service.decode_chunk(job_id, index)
        service.complete_job(job_id, 3)
        third = service.create_job(self.payload(seed=2028))
        self.assertTrue(third["ok"])

    def test_anchor_fault_returns_anchor_mismatch_and_releases_plan(self) -> None:
        service = PreviewService(self.root)
        payload = self.payload(frames=4, block_size=2)
        payload["fault_anchor"] = True
        start = service.create_job(payload)
        job_id = start["job"]["job_id"]

        with self.assertRaises(BusinessError) as captured:
            service.decode_chunk(job_id, 0)
        error = captured.exception
        self.assertEqual(error.code, ErrorCode.E_ANCHOR_MISMATCH)
        self.assertEqual(error.details["frame_index"], 0)
        self.assertIsNone(service.active_job_id)

    def test_anchor_strength_invalid_returns_config_error(self) -> None:
        service = PreviewService(self.root)
        with self.assertRaises(BusinessError) as captured:
            service.create_job(self.payload(anchor_strength=1.5))
        self.assertEqual(captured.exception.code, ErrorCode.E_CONFIG_INVALID)

    def test_first_chunk_actual_timer_enforces_budget(self) -> None:
        service = PreviewService(self.root)
        start = service.create_job(self.payload(latency_budget_ms=1000.0))
        job = service.get_job(start["job"]["job_id"])
        job.config = replace(job.config, latency_budget_ms=0.0001)

        with self.assertRaises(BusinessError) as captured:
            service.decode_chunk(job.job_id, 0)
        error = captured.exception
        self.assertEqual(error.code, ErrorCode.E_LATENCY_BUDGET)
        self.assertEqual(error.details["stage"], "first_chunk_decode")
        self.assertEqual(error.details["decoded_blocks"], 1)
        self.assertGreater(error.details["first_block_actual_ms"], 0.0)
        self.assertIn("first_chunk_decode", error.details["completed_stages"])
        self.assertIsNone(service.active_job_id)

    def test_concurrency_limit_rejects_excess_chunk_requests(self) -> None:
        service = PreviewService(self.root)
        start = service.create_job(
            self.payload(frames=6, block_size=2, max_concurrent_decode_tasks=2)
        )
        job_id = start["job"]["job_id"]
        with self.assertRaises(BusinessError) as captured:
            service.decode_chunks(job_id, [0, 1, 2])
        self.assertEqual(captured.exception.code, ErrorCode.E_RESOURCE_LIMIT)
        self.assertEqual(
            captured.exception.details["resource"], "concurrent_decode_tasks"
        )

        concurrent = service.decode_chunks(job_id, [0, 1])
        self.assertTrue(concurrent["ok"])
        self.assertEqual(len(concurrent["blocks"]), 2)
        service.decode_chunk(job_id, 2)
        completion = service.complete_job(job_id, 3)
        self.assertTrue(completion["consistency"]["equal"])

    def test_frame_limit_returns_resource_error(self) -> None:
        service = PreviewService(self.root)
        with self.assertRaises(BusinessError) as captured:
            service.create_job(self.payload(frames=9, max_frames=8))
        self.assertEqual(captured.exception.code, ErrorCode.E_RESOURCE_LIMIT)
        self.assertIsNone(service.active_job_id)

    def test_snapshot_restart_replacement_is_observable(self) -> None:
        service = PreviewService(self.root)
        result = self.demo_interactive(service, self.payload())
        version = result["completion"]["snapshot_versions"]["ready_blocks"]["version"]

        restarted = PreviewService(self.root)
        self.assertIsNone(restarted.startup_error)
        self.assertEqual(
            restarted.startup_snapshots["ready_blocks"]["loaded_version"], version
        )
        self.assertTrue(restarted.startup_snapshots["ready_blocks"]["replaced"])


if __name__ == "__main__":
    unittest.main()
