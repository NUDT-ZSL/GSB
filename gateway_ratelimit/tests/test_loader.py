import json
import unittest
from pathlib import Path

from gateway.errors import E_SPEC
from gateway.loader import load_requests, load_routes, load_token_table

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


class LoaderTest(unittest.TestCase):
    def test_loads_basic_sample(self) -> None:
        version, routes = load_routes(SAMPLES / "basic" / "routes.json")
        self.assertEqual(version, 1)
        self.assertEqual(len(routes), 2)
        table = load_token_table(SAMPLES / "basic" / "tokens.json")
        self.assertEqual(table.version, 1)
        requests = load_requests(SAMPLES / "basic" / "requests.ndjson", routes)
        self.assertEqual(len(requests), 12)
        self.assertTrue(all(request.ts_ms >= 0 for request in requests))

    def test_duplicate_route_rejected_with_line(self) -> None:
        with self.assertRaises(Exception) as ctx:
            load_routes(SAMPLES / "invalid" / "routes_duplicate.json")
        self.assertEqual(ctx.exception.code, E_SPEC)
        self.assertIsInstance(ctx.exception.line, int)
        self.assertGreater(ctx.exception.line, 0)

    def test_zero_window_rejected_at_load(self) -> None:
        with self.assertRaises(Exception) as ctx:
            load_routes(SAMPLES / "invalid" / "routes_bad_window.json")
        self.assertEqual(ctx.exception.code, E_SPEC)

    def test_non_monotonic_timestamps_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            load_requests(SAMPLES / "invalid" / "requests_non_monotonic.ndjson")
        self.assertEqual(ctx.exception.code, E_SPEC)
        self.assertEqual(ctx.exception.line, 2)

    def test_duplicate_token_rejected(self, tmp: Path | None = None) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            path.write_text(json.dumps({
                "version": 1,
                "tokens": [
                    {"token": "t", "client_id": "a", "expires_at_ms": 10},
                    {"token": "t", "client_id": "b", "expires_at_ms": 10},
                ],
                "signatures": [],
            }))
            with self.assertRaises(Exception) as ctx:
                load_token_table(path)
            self.assertEqual(ctx.exception.code, E_SPEC)


if __name__ == "__main__":
    unittest.main()
