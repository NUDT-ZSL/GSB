from __future__ import annotations

import threading
import time
import unittest

from pointcloud_workbench.errors import BusinessError
from pointcloud_workbench.scheduler import DeterministicClock, SchedulerJob, run_scheduler


class RecordingClock(DeterministicClock):
    def __init__(self) -> None:
        super().__init__()
        self.yield_points: list[int] = []

    def yield_round(self) -> None:
        super().yield_round()
        self.yield_points.append(self.units)


class SchedulerTests(unittest.TestCase):
    def test_memory_budget_admits_strict_index_order(self) -> None:
        jobs = [
            SchedulerJob(index=0, payload=0, sample_bytes=6, logical_cost=1),
            SchedulerJob(index=1, payload=1, sample_bytes=6, logical_cost=1),
            SchedulerJob(index=2, payload=2, sample_bytes=4, logical_cost=1),
            SchedulerJob(index=3, payload=3, sample_bytes=4, logical_cost=1),
        ]
        result = run_scheduler(
            jobs,
            lambda job: job.payload,
            max_workers=4,
            memory_budget_bytes=10,
            round_budget_units=100,
        )
        self.assertEqual(result.admission_sequence, [[0], [1, 2], [3]])
        self.assertLessEqual(result.peak_in_flight_bytes, 10)
        self.assertEqual(result.completed_indices, [0, 1, 2, 3])

    def test_concurrency_cap_is_never_exceeded(self) -> None:
        active = 0
        maximum = 0
        lock = threading.Lock()

        def work(job):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.005)
            with lock:
                active -= 1
            return job.index

        jobs = [
            SchedulerJob(index=index, payload=index, sample_bytes=1, logical_cost=1)
            for index in range(8)
        ]
        result = run_scheduler(
            jobs,
            work,
            max_workers=3,
            memory_budget_bytes=100,
            round_budget_units=100,
        )
        self.assertLessEqual(maximum, 3)
        self.assertEqual(len(result.completed), 8)

    def test_logical_round_budget_yields_deterministically(self) -> None:
        jobs = [
            SchedulerJob(index=index, payload=index, sample_bytes=1, logical_cost=4)
            for index in range(4)
        ]
        clock = RecordingClock()
        result = run_scheduler(
            jobs,
            lambda job: job.index,
            max_workers=1,
            memory_budget_bytes=10,
            round_budget_units=8,
            clock=clock,
        )
        self.assertEqual(result.admission_sequence, [[0], [1], [2], [3]])
        self.assertEqual(len(result.rounds), 2)
        self.assertEqual(result.rounds[0]["elapsed_units"], 8)
        self.assertTrue(result.rounds[0]["yielded"])
        self.assertEqual(clock.yields, 1)
        self.assertEqual(clock.yield_points, [8])

    def test_admission_sequence_is_reproducible_despite_completion_timing(self) -> None:
        jobs = [
            SchedulerJob(index=index, payload=index, sample_bytes=5, logical_cost=2)
            for index in range(7)
        ]

        def work(job):
            time.sleep(0.001 * ((job.index * 3) % 4))
            return job.index

        first = run_scheduler(
            jobs,
            work,
            max_workers=3,
            memory_budget_bytes=11,
            round_budget_units=4,
        )
        second = run_scheduler(
            jobs,
            work,
            max_workers=3,
            memory_budget_bytes=11,
            round_budget_units=4,
        )
        self.assertEqual(first.admission_sequence, second.admission_sequence)
        self.assertEqual(first.rounds, second.rounds)

    def test_first_error_cancels_pending_blocks(self) -> None:
        jobs = [
            SchedulerJob(index=index, payload=index, sample_bytes=1, logical_cost=1)
            for index in range(6)
        ]
        started: list[int] = []
        lock = threading.Lock()

        def work(job):
            with lock:
                started.append(job.index)
            if job.index == 1:
                raise BusinessError("E_TEST_BLOCK", "injected block failure")
            return job.index

        result = run_scheduler(
            jobs,
            work,
            max_workers=2,
            memory_budget_bytes=10,
            round_budget_units=100,
        )
        self.assertEqual(result.errors[0].code, "E_TEST_BLOCK")
        self.assertEqual(result.admission_sequence, [[0, 1]])
        self.assertEqual(result.pending_indices, [2, 3, 4, 5])
        self.assertNotIn(2, started)

    def test_stop_event_only_stops_after_admitted_wave(self) -> None:
        jobs = [
            SchedulerJob(index=index, payload=index, sample_bytes=1, logical_cost=1)
            for index in range(6)
        ]
        stop_event = threading.Event()

        def work(job):
            if job.index == 0:
                stop_event.set()
            return job.index

        result = run_scheduler(
            jobs,
            work,
            max_workers=2,
            memory_budget_bytes=10,
            round_budget_units=100,
            stop_event=stop_event,
        )
        self.assertTrue(result.interrupted)
        self.assertEqual(result.admission_sequence, [[0, 1]])
        self.assertEqual(result.completed_indices, [0, 1])
        self.assertEqual(result.pending_indices, [2, 3, 4, 5])

    def test_individual_block_larger_than_budget_is_rejected(self) -> None:
        jobs = [SchedulerJob(index=0, payload=0, sample_bytes=20, logical_cost=1)]
        with self.assertRaises(BusinessError) as caught:
            run_scheduler(
                jobs,
                lambda job: job.index,
                max_workers=1,
                memory_budget_bytes=10,
                round_budget_units=10,
            )
        self.assertEqual(caught.exception.code, "E_BUDGET")

