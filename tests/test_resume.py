from __future__ import annotations

import tempfile
import unittest

from numeric_workbench.training import train_prepared
from tests.helpers import config_with, make_dataset, prepare_for


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path, self.dataset, self.split = make_dataset(self.temp.name)
        self.config = config_with(
            max_iterations=14,
            convergence_tolerance=1e-9,
            min_improvement=0.0,
            chunk_size=24,
        )
        self.prepared = prepare_for(self.dataset, self.split, self.config)

    def tearDown(self):
        self.temp.cleanup()

    def _train(self, **kwargs):
        return train_prepared(
            self.prepared,
            self.config,
            reduce_mode="chunked",
            run_id="resume-run",
            collect_chunk_details=False,
            **kwargs,
        )

    def test_round_boundary_resume_is_byte_identical(self):
        uninterrupted = self._train()
        interrupted = self._train(stop_after_rounds=5)
        resumed = self._train(initial_state=interrupted.checkpoint_state())
        self.assertEqual(interrupted.stop_reason, "interrupted_at_round_boundary")
        self.assertEqual(resumed.started_from_round, 6)
        self.assertEqual(interrupted.committed_rounds, [1, 2, 3, 4, 5])
        self.assertEqual(resumed.rebuilt_rounds, list(range(6, 15)))
        self.assertEqual(uninterrupted.core_digest, resumed.core_digest)
        self.assertEqual(uninterrupted.losses_hex, resumed.losses_hex)
        self.assertEqual(uninterrupted.params_hex, resumed.params_hex)
        self.assertEqual(
            uninterrupted.validation_trajectory, resumed.validation_trajectory
        )

    def test_unfinished_round_is_discarded_and_rebuilt(self):
        uninterrupted = self._train()
        interrupted = self._train(interrupt_round=4, interrupt_after_chunk=2)
        self.assertEqual(interrupted.stop_reason, "interrupted_mid_round")
        self.assertEqual(interrupted.discarded_round, 4)
        self.assertEqual(interrupted.committed_rounds, [1, 2, 3])
        resumed = self._train(initial_state=interrupted.checkpoint_state())
        self.assertEqual(resumed.started_from_round, 4)
        self.assertEqual(resumed.rebuilt_rounds, list(range(4, 15)))
        self.assertEqual(uninterrupted.core_digest, resumed.core_digest)


if __name__ == "__main__":
    unittest.main()
