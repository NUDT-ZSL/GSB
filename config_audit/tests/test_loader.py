import json
import tempfile
import unittest
from pathlib import Path

from config_audit.errors import ConfigError
from config_audit.loader import load_archives


def _write(directory: Path, name: str, payload: dict) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _valid(self, node="n1", version="v1"):
        return {
            "node_id": node, "version": version,
            "items": [
                {"key": "http.timeout", "value": 100, "unit": "ms",
                 "source": "manual"},
                {"key": "backoff.budget", "unit": "ms", "source": "derived",
                 "formula": {"coefficients": {"http.timeout": 2},
                             "constant": 5}},
            ],
        }

    def test_valid_load(self):
        _write(self.dir, "a.json", self._valid())
        archives = load_archives([self.dir / "a.json"])
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].keys,
                         ("backoff.budget", "http.timeout"))

    def test_whole_batch_rejected_with_locations(self):
        bad = {"node_id": "x", "version": "v1", "items": [
            {"key": "BAD", "value": 1, "unit": "ms", "source": "manual"},
            {"key": "http.timeout", "value": 1, "unit": "ms",
             "source": "manual"},
            {"key": "http.timeout", "value": 2, "unit": "ms",
             "source": "manual"},
        ]}
        _write(self.dir, "bad.json", bad)
        with self.assertRaises(ConfigError) as ctx:
            load_archives([self.dir / "bad.json"])
        self.assertEqual(ctx.exception.code, "E_CONFIG")
        self.assertTrue(any("illegal key" in loc for loc in
                            ctx.exception.locations))
        self.assertTrue(any("duplicate key" in loc for loc in
                            ctx.exception.locations))

    def test_missing_upstream_rejected(self):
        payload = self._valid()
        payload["items"][1]["formula"]["coefficients"] = {"ghost.key": 1}
        _write(self.dir, "a.json", payload)
        with self.assertRaises(ConfigError) as ctx:
            load_archives([self.dir / "a.json"])
        self.assertTrue(any("missing upstream" in loc for loc in
                            ctx.exception.locations))

    def test_inconsistent_units_across_nodes(self):
        _write(self.dir, "a.json", self._valid("n1"))
        other = self._valid("n2")
        other["items"][0]["unit"] = "s"
        _write(self.dir, "b.json", other)
        with self.assertRaises(ConfigError) as ctx:
            load_archives([self.dir / "a.json", self.dir / "b.json"])
        self.assertTrue(any("inconsistent units" in loc for loc in
                            ctx.exception.locations))

    def test_boolean_value_rejected(self):
        payload = self._valid()
        payload["items"][0]["value"] = True
        _write(self.dir, "a.json", payload)
        with self.assertRaises(ConfigError):
            load_archives([self.dir / "a.json"])


if __name__ == "__main__":
    unittest.main()
