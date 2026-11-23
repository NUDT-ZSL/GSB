import unittest

from xrel.config import AppConfig, Bm25Config, ExpandConfig, ThresholdConfig
from xrel.errors import ConfigInvalid, XrelError
from xrel.numerics import FixedConfig, RoundingMode


class ConfigValidationTests(unittest.TestCase):
    def test_negative_fixed_scale_raises_config_invalid(self):
        config = AppConfig(
            precision="fixed",
            fixed=FixedConfig(scale=-5, rounding=RoundingMode.HALF_EVEN),
        )
        with self.assertRaises(ConfigInvalid) as ctx:
            config.validate()
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(ctx.exception.context["field"], "scale")

    def test_unknown_rounding_mode_raises_config_invalid(self):
        with self.assertRaises(ConfigInvalid):
            FixedConfig(scale=10, rounding="weird").validate()  # type: ignore[arg-type]

    def test_expand_and_bm25_bounds(self):
        with self.assertRaises(ConfigInvalid):
            ExpandConfig(max_candidates=0).validate()
        with self.assertRaises(ConfigInvalid):
            ExpandConfig(min_confidence=1.5).validate()
        with self.assertRaises(ConfigInvalid):
            Bm25Config(b=2.0).validate()

    def test_threshold_business_codes(self):
        with self.assertRaises(XrelError) as strategy:
            ThresholdConfig(strategy="bad").validate()
        self.assertEqual(strategy.exception.code, "E_THRESHOLD_STRATEGY_INVALID")
        with self.assertRaises(XrelError) as tolerance:
            ThresholdConfig(tolerance=-0.1).validate()
        self.assertEqual(tolerance.exception.code, "E_THRESHOLD_TOLERANCE_INVALID")


if __name__ == "__main__":
    unittest.main()
