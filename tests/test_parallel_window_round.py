from __future__ import annotations

import unittest

from radapt.aggregate import ExactTruthPath, estimate_memory_bytes, estimate_logical_ms
from radapt.generator import generate_batch
from radapt.parsing import parse_bundle
from radapt.sample import sample_raw
from radapt.scheduler import ParallelScheduler, ScheduledTask


class ParallelWindowRoundTests(unittest.TestCase):
    def test_window_groups_run_in_one_round_and_match_truth(self) -> None:
        raw = sample_raw()
        bundle = parse_bundle(raw["table"], raw["plan"], raw["arrivals"], raw["config"])
        table = bundle.tables[0]
        envelopes = [
            generate_batch(table, table.global_seed, spec)
            for spec in bundle.arrivals.batches
            if spec.seq in {1, 3, 5}
        ]
        scheduler = ParallelScheduler(bundle.config)
        tasks = []
        expected = {}
        for envelope in envelopes:
            window = bundle.plan.window(envelope.spec.window_id)

            def work(window=window, envelope=envelope):
                from radapt.aggregate import WindowAggregate

                aggregate = WindowAggregate(window)
                aggregate.add_batch(envelope, table, operator="hash")
                return aggregate

            truth = ExactTruthPath(window, table)
            truth.consume_envelope(envelope)
            expected[window.window_id] = truth.canonical()
            tasks.append(
                ScheduledTask(
                    task_id=f"round-{window.window_id}",
                    window_id=window.window_id,
                    batch_seq=envelope.spec.seq,
                    logical_estimate_ms=estimate_logical_ms("hash", envelope.spec.row_count, 64),
                    memory_estimate_bytes=estimate_memory_bytes(table, envelope.spec.row_count, 64, len(window.aggregations)),
                    fn=work,
                )
            )

        report = scheduler.run_round(tasks)
        self.assertEqual(len(report.completed), 3)
        self.assertFalse(report.failed_after_start)
        for outcome in report.completed:
            self.assertEqual(outcome.result.canonical(), expected[outcome.window_id])


if __name__ == "__main__":
    unittest.main()
