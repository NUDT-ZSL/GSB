import unittest

import numpy as np

from gbtdiag import (
    TrainConfig,
    make_regression_dataset,
    predict,
    train,
)
from gbtdiag.boosting import recompute_prediction
from gbtdiag.io_utils import stable_hash


class TrainingTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(
            n_trees=15,
            max_depth=3,
            subsample=0.75,
            colsample=0.8,
            seed=123,
        )
        self.dataset = make_regression_dataset(n_rows=300, seed=123)

    def test_byte_identical_runs(self):
        first = train(self.dataset, self.config)
        second = train(self.dataset, self.config)
        self.assertEqual(
            stable_hash(first.to_report()), stable_hash(second.to_report())
        )

    def test_round_details_recompute_loss_and_prediction(self):
        model = train(self.dataset, self.config)
        self.assertEqual(len(model.round_details), self.config.n_trees)
        losses = [detail.loss_after for detail in model.round_details]
        self.assertLess(losses[-1], losses[0])
        # Recomputed prediction from tree details matches stored prediction.
        rebuilt = recompute_prediction(model)
        self.assertEqual(
            stable_hash(rebuilt), stable_hash(model.final_prediction)
        )

    def test_sampling_sequences_are_explicit_and_reproducible(self):
        first = train(self.dataset, self.config)
        second = train(self.dataset, self.config)
        for detail_a, detail_b in zip(
            first.round_details, second.round_details
        ):
            self.assertEqual(detail_a.row_sample, detail_b.row_sample)
            self.assertEqual(detail_a.col_sample, detail_b.col_sample)
            # Row samples are sorted subsets within the valid range.
            self.assertEqual(
                detail_a.row_sample, sorted(detail_a.row_sample)
            )

    def test_predict_entry_point_matches_training_prediction(self):
        model = train(self.dataset, self.config)
        inference = predict(model, self.dataset.X)
        self.assertTrue(
            np.allclose(inference, model.final_prediction, atol=1e-12)
        )

    def test_tree_structure_hashes_are_stable(self):
        first = train(self.dataset, self.config)
        second = train(self.dataset, self.config)
        for detail_a, detail_b in zip(
            first.round_details, second.round_details
        ):
            self.assertEqual(
                stable_hash(detail_a.tree.to_dict()),
                stable_hash(detail_b.tree.to_dict()),
            )


if __name__ == "__main__":
    unittest.main()
