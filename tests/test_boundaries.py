from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hotel_edge_kv import (
    CancellationEvent,
    EdgeConfig,
    Scheduler,
    ServiceRequest,
    audit_scheduler,
    generate_scenario,
    run_pipeline,
)


def make_config(**overrides: int) -> EdgeConfig:
    values = {
        "block_size_tokens": 2,
        "total_blocks": 8,
        "block_bytes": 100,
        "vram_budget_bytes": 800,
        "max_batch_requests": 2,
        "decode_step_ticks": 2,
        "prefill_duration_ticks": 1,
        "seed": 7,
    }
    values.update(overrides)
    return EdgeConfig(**values)


def make_request(
    request_id: str,
    sequence: int,
    arrival: int,
    max_new_tokens: int,
    prompt_tokens: int = 4,
) -> ServiceRequest:
    prefix_length = prompt_tokens // 2
    suffix_length = prompt_tokens - prefix_length
    number = int(request_id.split("-")[-1])
    return ServiceRequest(
        request_id=request_id,
        ticket_id=f"WO-TEST-{number:04d}",
        room_no=f"{number % 20 + 1:04d}",
        arrival_time=arrival,
        arrival_seq=sequence,
        prompt_prefix="酒店:测试|楼层:03|房型:大床房|",
        prefix_key="TEST-03-DK",
        prefix_token_ids=tuple(range(100, 100 + prefix_length)),
        suffix_text="请送水",
        suffix_token_ids=tuple(range(200, 200 + suffix_length)),
        max_new_tokens=max_new_tokens,
    )


def make_cancel(
    event_id: str,
    request: ServiceRequest,
    time: int,
    reason: str,
    sequence: int,
) -> CancellationEvent:
    return CancellationEvent(
        event_id=event_id,
        request_id=request.request_id,
        ticket_id=request.ticket_id,
        time=time,
        reason=reason,
        sequence=sequence,
    )


class SchedulerBoundaryTests(unittest.TestCase):
    def test_block_conservation_budget_and_clean_audit(self) -> None:
        config = make_config(
            total_blocks=10,
            block_bytes=64,
            vram_budget_bytes=512,
            max_batch_requests=3,
        )
        requests = [
            make_request("REQ-0001", 0, 0, 3),
            make_request("REQ-0002", 1, 0, 2),
            make_request("REQ-0003", 2, 1, 4),
        ]
        scheduler = Scheduler(config, requests, [])
        scheduler.run()

        self.assertTrue(scheduler.all_terminal)
        self.assertEqual(scheduler.used_blocks, 0)
        for sample in scheduler.memory_samples:
            self.assertEqual(
                sample["used_blocks"] + sample["free_blocks"],
                config.total_blocks,
            )
            self.assertLessEqual(sample["used_bytes"], config.vram_budget_bytes)
        self.assertEqual(audit_scheduler(scheduler)["status"], "ok")

    def test_cancel_wins_over_completion_at_same_tick(self) -> None:
        config = make_config()
        request = make_request("REQ-0001", 0, 0, 1)
        cancellation = make_cancel(
            "CXL-0001", request, 2, "handled_by_staff", 10
        )
        scheduler = Scheduler(config, [request], [cancellation])
        scheduler.run()

        runtime = scheduler.runtimes[request.request_id]
        self.assertEqual(runtime.status, "cancelled")
        self.assertEqual(runtime.tokens_generated, 0)
        self.assertEqual(runtime.cancel_stage, "decode")
        self.assertEqual(runtime.terminal_at, 2)
        self.assertEqual(len(scheduler.competition_records), 1)
        competition = scheduler.competition_records[0]
        self.assertIn("cancel_vs_completion", competition["types"])
        self.assertEqual(competition["completion_would_occur_at"], 2)
        self.assertEqual(audit_scheduler(scheduler)["status"], "ok")

    def test_multiple_cancellations_use_earliest_sequence(self) -> None:
        config = make_config()
        request = make_request("REQ-0001", 0, 0, 1)
        later = make_cancel("CXL-0002", request, 2, "wrong_order", 20)
        earlier = make_cancel(
            "CXL-0001", request, 2, "guest_changed_mind", 5
        )
        scheduler = Scheduler(config, [request], [later, earlier])
        scheduler.run()

        runtime = scheduler.runtimes[request.request_id]
        self.assertEqual(runtime.cancel_reason, "guest_changed_mind")
        self.assertEqual(runtime.cancel_event_id, "CXL-0001")
        self.assertEqual(len(scheduler.competition_records), 1)
        competition = scheduler.competition_records[0]
        self.assertIn("multiple_cancellations", competition["types"])
        self.assertEqual(
            competition["rejected_events"][0]["event_id"], "CXL-0002"
        )

    def test_prefill_cancellation_releases_all_blocks_immediately(self) -> None:
        config = make_config(max_batch_requests=1)
        request = make_request("REQ-0001", 0, 0, 3)
        cancellation = make_cancel("CXL-0001", request, 0, "wrong_order", 1)
        scheduler = Scheduler(config, [request], [cancellation])
        scheduler.run()

        runtime = scheduler.runtimes[request.request_id]
        self.assertEqual(runtime.status, "cancelled")
        self.assertEqual(runtime.cancel_stage, "prefill")
        self.assertEqual(runtime.reclaimed_at, 0)
        self.assertEqual(runtime.tokens_generated, 0)
        self.assertEqual(scheduler.used_blocks, 0)
        self.assertEqual(scheduler.cancellation_records[0]["latency_ticks"], 0)

    def test_decode_cancel_waits_for_next_boundary_and_stops_tokens(self) -> None:
        config = make_config(max_batch_requests=1)
        request = make_request("REQ-0001", 0, 0, 3)
        cancellation = make_cancel("CXL-0001", request, 3, "wrong_order", 1)
        scheduler = Scheduler(config, [request], [cancellation])
        scheduler.run()

        runtime = scheduler.runtimes[request.request_id]
        self.assertEqual(runtime.status, "cancelled")
        self.assertEqual(runtime.cancel_effective_at, 3)
        self.assertEqual(runtime.reclaimed_at, 4)
        self.assertEqual(runtime.tokens_generated, 1)
        self.assertTrue(
            all(event["time"] < 3 for event in scheduler.token_events)
        )
        self.assertEqual(scheduler.cancellation_records[0]["latency_ticks"], 1)

    def test_cancelled_blocks_are_reused_by_queued_request(self) -> None:
        config = make_config(max_batch_requests=1)
        cancelled = make_request("REQ-0001", 0, 0, 2)
        queued = make_request("REQ-0002", 1, 0, 2)
        cancellation = make_cancel(
            "CXL-0001", cancelled, 0, "guest_changed_mind", 1
        )
        scheduler = Scheduler(config, [cancelled, queued], [cancellation])
        scheduler.run()

        cancelled_runtime = scheduler.runtimes[cancelled.request_id]
        queued_runtime = scheduler.runtimes[queued.request_id]
        self.assertEqual(cancelled_runtime.status, "cancelled")
        self.assertEqual(queued_runtime.status, "completed")
        links = [
            link
            for link in scheduler.reuse_links
            if link["from_request_id"] == cancelled.request_id
            and link["to_request_id"] == queued.request_id
        ]
        self.assertEqual(len(links), cancelled_runtime.prefill_blocks_allocated)
        self.assertTrue(all(link["from_was_cancelled"] for link in links))
        self.assertEqual(audit_scheduler(scheduler)["status"], "ok")

    def test_checkpoint_restores_decode_cancellation_pending_state(self) -> None:
        config = make_config(max_batch_requests=1)
        request = make_request("REQ-0001", 0, 0, 3)
        cancellation = make_cancel("CXL-0001", request, 3, "wrong_order", 1)

        prefix = Scheduler(config, [request], [cancellation])
        prefix.run_until(3)
        self.assertEqual(prefix.runtimes[request.request_id].status, "cancel_pending")
        checkpoint = prefix.to_checkpoint()

        restored = Scheduler.from_checkpoint(checkpoint)
        self.assertEqual(prefix.occupancy_snapshot(), restored.occupancy_snapshot())
        restored.run()

        reference = Scheduler(config, [request], [cancellation])
        reference.run()
        self.assertEqual(
            [entry.to_dict() for entry in restored.lifecycle],
            [entry.to_dict() for entry in reference.lifecycle],
        )
        self.assertEqual(
            restored.occupancy_snapshot(), reference.occupancy_snapshot()
        )
        self.assertEqual(
            restored.runtimes[request.request_id].reclaimed_at, 4
        )

    def test_dense_timeline_is_idle_before_first_arrival(self) -> None:
        config = make_config()
        request = make_request("REQ-0001", 0, 5, 1)
        scheduler = Scheduler(config, [request], [])
        scheduler.run()
        timeline = scheduler.dense_memory_timeline()

        self.assertEqual(timeline[0]["time"], 0)
        for sample in timeline[:5]:
            self.assertEqual(sample["used_blocks"], 0)
            self.assertEqual(sample["free_blocks"], config.total_blocks)
        self.assertTrue(
            any(sample["used_blocks"] > 0 for sample in timeline)
        )


class PipelineTests(unittest.TestCase):
    def test_generated_pipeline_and_resume_match(self) -> None:
        scenario = generate_scenario()
        with tempfile.TemporaryDirectory() as directory:
            summary = run_pipeline(scenario, output_dir=directory, resume_at=20)
            root = Path(directory)
            self.assertEqual(summary["acceptance"]["status"], "ok")
            self.assertTrue(summary["resume"]["snapshot_equal"])
            self.assertTrue(summary["resume"]["matches_reference"])
            for relative in (
                "summary.json",
                "summary.md",
                "ledger/lifecycle.json",
                "ledger/block_ledger.json",
                "reports/cancellation_report.json",
                "reports/competition_report.json",
                "reports/memory_timeline.json",
                "reports/audit_report.json",
                "reports/resume_report.json",
                "checkpoint/resume_checkpoint.json",
            ):
                self.assertTrue((root / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
