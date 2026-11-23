import unittest
from dataclasses import replace

from gbtdiag import TrainConfig
from gbtdiag.errors import DomainError, ErrorCode


class ConfigTests(unittest.TestCase):
    def test_invalid_stage_tolerances_rejected(self):
        config = replace(
            TrainConfig(), stage_tolerances=(1e-9, 1e-9, 1e-9, 1e-9)
        )
        with self.assertRaises(DomainError) as context:
            config.validate_basic()
        self.assertEqual(
            context.exception.code, ErrorCode.CONFIG_INVALID
        )
        self.assertEqual(
            context.exception.field, "stage_tolerances"
        )

    def test_named_rng_streams_are_distinct_and_deterministic(self):
        config = TrainConfig(seed=99)
        first = config.rng("row_sample").normal(size=4)
        second = config.rng("row_sample").normal(size=4)
        other = config.rng("col_sample").normal(size=4)
        self.assertTrue((first == second).all())
        self.assertFalse((first == other).all())

    def test_unknown_rng_stream_rejected(self):
        with self.assertRaises(DomainError):
            TrainConfig(seed=1).rng("not_a_stream")

    def test_default_floor_invalid_reports_field(self):
        config = replace(
            TrainConfig(), hessian_floor_default=float("-inf")
        )
        with self.assertRaises(DomainError) as context:
            config.validate_basic()
        self.assertEqual(
            context.exception.field, "hessian_floor_default"
        )


if __name__ == "__main__":
    unittest.main()
