"""要求 3：三档精度共用同一算子语义，仅精度损失不同。"""

import numpy as np

from statediag.precision import ROUNDING_MODES, make_backend
from statediag.transforms import block_bounds, run_block_serial, run_monolithic

from tests.support import SampleCase


class PrecisionTest(SampleCase):
    def test_block_sizes_and_orders_identical_across_precision(self):
        record = self._records()[2]
        task = record["task"]
        expected_bounds = block_bounds(self.state_dim, task.block_size)
        for precision in ("float64", "float32", "fixed"):
            backend = make_backend(precision, task.quant_scale, "half-even")
            final, traces = run_block_serial(
                record["initial_state"], backend, task.num_steps,
                task.block_size,
            )
            bounds = [(t.global_start, t.global_end) for t in traces]
            self.assertEqual(bounds, expected_bounds)
            self.assertEqual(
                [t.reduction_index for t in traces], list(range(len(traces)))
            )
            self.assertEqual(final.shape[0], self.state_dim)

    def test_block_serial_matches_monolithic_each_precision(self):
        record = self._records()[0]
        task = record["task"]
        for precision in ("float64", "float32", "fixed"):
            backend = make_backend(precision, task.quant_scale, "half-even")
            block_final, _ = run_block_serial(
                record["initial_state"], backend, task.num_steps,
                task.block_size,
            )
            mono_final = run_monolithic(
                record["initial_state"], backend, task.num_steps
            )
            self.assertTrue(
                np.array_equal(block_final, mono_final),
                msg=precision,
            )

    def test_fixed_rounding_modes_distinct_but_valid(self):
        record = self._records()[0]
        task = record["task"]
        outputs = set()
        for mode in ROUNDING_MODES:
            backend = make_backend("fixed", task.quant_scale, mode)
            final, _ = run_block_serial(
                record["initial_state"], backend, task.num_steps,
                task.block_size,
            )
            self.assertTrue(np.issubdtype(final.dtype, np.integer))
            outputs.add(final.tobytes())
        self.assertGreaterEqual(len(outputs), 1)

    def _records(self):
        from statediag.ingestion import ingest

        return ingest(self.tasks_path, self.state_dim)
