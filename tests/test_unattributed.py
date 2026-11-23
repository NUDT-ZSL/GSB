import unittest

from gbtdiag import (
    TrainConfig,
    make_injection,
    make_regression_dataset,
    train,
)
from gbtdiag.errors import ErrorCode


class UnattributedFaultTests(unittest.TestCase):
    def test_raw_fault_is_recorded_not_swallowed(self):
        config = TrainConfig(n_trees=5, seed=23)
        dataset = make_regression_dataset(n_rows=200, seed=23)
        specs = [
            make_injection(
                "raw_fault",
                stage="gain",
                round_index=1,
            )
        ]
        model = train(dataset, config, injection_specs=specs)
        unattributed = [
            record
            for record in model.records
            if record["code"] == ErrorCode.UNATTRIBUTED.value
        ]
        self.assertEqual(len(unattributed), 1)
        self.assertIn("gain", unattributed[0]["stage"])
        # The faulty round degrades to a zero-contribution placeholder tree.
        self.assertTrue(model.round_details[1].tree.skipped)
        # Training still completes and the remaining rounds stay intact.
        self.assertEqual(len(model.trees), 5)

    def test_guard_boundary_converts_unexpected_exception(self):
        from gbtdiag.boosting import _guard_stage
        from gbtdiag.records import unattributed_record

        def boom():
            raise ValueError("boom")

        value, records = _guard_stage(
            "gain", 0, boom, sample_indices=[1, 2]
        )
        self.assertIsNone(value)
        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["code"], ErrorCode.UNATTRIBUTED.value
        )
        self.assertEqual(records[0]["sample_indices"], [1, 2])

        record = unattributed_record(
            "leaf", RuntimeError("x"), sample_indices=[4]
        )
        self.assertEqual(
            record["code"], ErrorCode.UNATTRIBUTED.value
        )


if __name__ == "__main__":
    unittest.main()
