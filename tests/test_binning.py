import unittest

from gbtdiag import TrainConfig, make_regression_dataset
from gbtdiag.binning import assign_bins, build_binnings
from gbtdiag.errors import ErrorCode
from gbtdiag.injection import InjectionController, make_injection
from gbtdiag.io_utils import stable_hash


class BinningTests(unittest.TestCase):
    def setUp(self):
        self.config = TrainConfig(n_thresholds=8, seed=11)
        self.dataset = make_regression_dataset(
            n_rows=200, n_numeric=4, n_categorical=2, seed=11
        )

    def test_deterministic_byte_identical_binnings(self):
        first = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        second = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        self.assertEqual(stable_hash(first.to_dict()), stable_hash(second.to_dict()))

    def test_candidate_ordering_break_ties(self):
        result = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        # Feature index ascending, then threshold ascending.
        keys = [(fi, t) for fi, t in result.ordered_candidates]
        self.assertEqual(keys, sorted(keys))
        for binning in result.feature_binnings:
            self.assertEqual(
                list(binning.candidates), sorted(binning.candidates)
            )

    def test_categorical_fixed_encoding(self):
        result = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        categorical = [
            fb for fb in result.feature_binnings if fb.is_categorical
        ]
        self.assertEqual(len(categorical), 2)
        for binning, card in zip(
            categorical, self.dataset.category_cardinalities
        ):
            self.assertEqual(binning.cardinality, card)
            self.assertEqual(
                list(binning.candidates), list(range(card - 1))
            )

    def test_numeric_edges_and_bin_assignment(self):
        result = build_binnings(
            self.dataset.X,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        numeric = next(
            fb for fb in result.feature_binnings if not fb.is_categorical
        )
        self.assertEqual(numeric.edges[0], float("-inf"))
        self.assertEqual(numeric.edges[-1], float("inf"))
        self.assertEqual(
            list(numeric.edges[1:-1]), list(numeric.candidates)
        )
        bins = assign_bins(self.dataset.X, numeric)
        self.assertEqual(bins.min(), 0)
        self.assertLessEqual(
            bins.max(), len(numeric.candidates)
        )

    def test_category_out_of_range_detection(self):
        specs = [
            make_injection(
                "category_oor",
                feature_index=4,
                sample_indices=[3, 7, 12],
                value=999,
            )
        ]
        controller = InjectionController(specs, self.config, self.dataset.n_rows)
        categorical_features = [
            j for j, flag in enumerate(self.dataset.categorical_mask) if flag
        ]
        injected = controller.apply_binning_injections(
            self.dataset.X, categorical_features
        )
        result = build_binnings(
            injected,
            self.dataset.categorical_mask,
            self.dataset.category_cardinalities,
            self.config,
        )
        self.assertEqual(
            result.records[0]["code"],
            ErrorCode.CATEGORY_OUT_OF_RANGE.value,
        )
        self.assertEqual(result.records[0]["sample_indices"], [3, 7, 12])
        self.assertEqual(result.excluded_rows, [3, 7, 12])
        self.assertEqual(
            result.records[0]["degradation"]["category"], "row_quarantine"
        )


if __name__ == "__main__":
    unittest.main()
