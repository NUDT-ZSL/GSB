import unittest

from gateway import selector
from gateway.loader import load_routes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NumpyParityTest(unittest.TestCase):
    def test_numpy_and_scalar_estimates_agree(self) -> None:
        try:
            import numpy  # noqa: F401
        except Exception:
            self.skipTest("numpy not installed in this environment")
        _, routes = load_routes(ROOT / "samples" / "large" / "routes.json")
        route = routes[0]
        with_numpy = selector.estimate_workloads(route, 120, 3000, 5)
        original = selector._numpy_available
        try:
            selector._numpy_available = lambda: False
            scalar = selector.estimate_workloads(route, 120, 3000, 5)
        finally:
            selector._numpy_available = original
        self.assertEqual(with_numpy, scalar)


if __name__ == "__main__":
    unittest.main()
