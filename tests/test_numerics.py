import math
import unittest

from xrel.errors import ConfigInvalid
from xrel.numerics import (
    FixedBackend,
    FixedConfig,
    Float32Backend,
    RoundingMode,
    make_backend,
    parse_fixed_config,
    round_int,
)


class RoundingTests(unittest.TestCase):
    def test_rounding_modes_are_deterministic(self):
        self.assertEqual(round_int(2.5, RoundingMode.HALF_EVEN), 2)
        self.assertEqual(round_int(3.5, RoundingMode.HALF_EVEN), 4)
        self.assertEqual(round_int(2.5, RoundingMode.HALF_UP), 3)
        self.assertEqual(round_int(2.9, RoundingMode.TRUNCATE), 2)
        self.assertEqual(round_int(2.1, RoundingMode.FLOOR), 2)
        self.assertEqual(round_int(2.1, RoundingMode.CEILING), 3)


class BackendTests(unittest.TestCase):
    def test_fixed_quantize_scales_integer_statistics(self):
        backend = FixedBackend(FixedConfig(1000, RoundingMode.HALF_EVEN))
        self.assertEqual(backend.quantize(3), 3000)
        self.assertAlmostEqual(backend.to_float(backend.quantize(2.718)), 2.718)

    def test_fixed_mul_div_round_trip(self):
        backend = FixedBackend(FixedConfig(1_000_000, RoundingMode.HALF_EVEN))
        a = backend.quantize(1.5)
        b = backend.quantize(2.0)
        self.assertAlmostEqual(backend.to_float(backend.mul(a, b)), 3.0, places=5)
        ratio = backend.div(backend.quantize(28), backend.quantize(27.4))
        self.assertAlmostEqual(backend.to_float(ratio), 28 / 27.4, places=5)

    def test_fixed_log_matches_float_within_quantum(self):
        backend = FixedBackend(FixedConfig(1_000_000, RoundingMode.HALF_EVEN))
        value = backend.quantize(4.0)
        self.assertAlmostEqual(
            backend.to_float(backend.log(value)), math.log(4.0), places=5
        )

    def test_float32_flushes_subnormal(self):
        backend = Float32Backend()
        tiny = backend.quantize(1e-40)
        self.assertEqual(backend.to_float(tiny), 0.0)
        self.assertTrue(backend.is_subnormal_real(1e-40))

    def test_invalid_scale_and_rounding_raise_config_error(self):
        with self.assertRaises(ConfigInvalid):
            parse_fixed_config({"scale": 0, "rounding": "half_even"})
        with self.assertRaises(ConfigInvalid):
            parse_fixed_config({"scale": 100, "rounding": "unknown"})
        with self.assertRaises(ConfigInvalid):
            make_backend("fixed", None)
        with self.assertRaises(ConfigInvalid):
            make_backend("bfloat16")


if __name__ == "__main__":
    unittest.main()
