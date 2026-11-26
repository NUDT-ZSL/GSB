"""要求 2：分块变换、补偿求和、跨块归约与轨迹可追溯。"""

import numpy as np

from statediag.precision import make_backend
from statediag.transforms import (
    block_bounds,
    cross_block_reduce,
    run_block_serial,
    run_monolithic,
)

from tests.support import SampleCase


class TransformTest(SampleCase):
    def test_block_serial_matches_monolithic_float64(self):
        for record in ingest_all(self):
            task = record["task"]
            backend = make_backend("float64")
            block_final, _ = run_block_serial(
                record["initial_state"], backend, task.num_steps,
                task.block_size,
            )
            mono_final = run_monolithic(
                record["initial_state"], backend, task.num_steps
            )
            self.assertEqual(block_final.dtype, np.float64)
            self.assertTrue(np.array_equal(block_final, mono_final))

    def test_traces_traceable_to_block_and_reduction(self):
        record = ingest_all(self)[0]
        task = record["task"]
        backend = make_backend("float64")
        final, traces = run_block_serial(
            record["initial_state"], backend, task.num_steps, task.block_size
        )
        reduction = cross_block_reduce(final, traces, backend)
        self.assertEqual(
            reduction["order"], list(range(len(block_bounds(
                self.state_dim, task.block_size))))
        )
        for trace in traces:
            self.assertEqual(trace.block_index, trace.reduction_index)
            self.assertEqual(
                trace.global_end - trace.global_start, task.block_size
            )

    def test_reduction_order_deterministic(self):
        record = ingest_all(self)[1]
        task = record["task"]
        backend = make_backend("float64")
        final_a, traces_a = run_block_serial(
            record["initial_state"], backend, task.num_steps, task.block_size
        )
        final_b, traces_b = run_block_serial(
            record["initial_state"], backend, task.num_steps, task.block_size
        )
        red_a = cross_block_reduce(final_a, traces_a, backend)
        red_b = cross_block_reduce(final_b, traces_b, backend)
        self.assertEqual(red_a, red_b)


def ingest_all(case):
    from statediag.ingestion import ingest

    return ingest(case.tasks_path, case.state_dim)
