from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from .helpers import SAMPLES, cli, parse_json


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.state = self.base / "state"
        self.model = SAMPLES / "model.json"
        self.replicas = SAMPLES / "replicas.json"
        self.requests = SAMPLES / "requests.json"
        self.precision = SAMPLES / "precision.json"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write(self, name: str, value: object) -> Path:
        path = self.base / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def _load(self, *, replicas=None, requests=None, precision=None):
        return cli(
            self.state,
            "load",
            "--model",
            str(self.model),
            "--replicas",
            str(replicas or self.replicas),
            "--requests",
            str(requests or self.requests),
            "--precision",
            str(precision or self.precision),
            "--reset",
        )

    def test_duplicate_replica_fails_without_state(self) -> None:
        replicas = json.loads(self.replicas.read_text(encoding="utf-8"))
        replicas["replicas"].append(dict(replicas["replicas"][0]))
        result = self._load(replicas=self._write("duplicate-replicas.json", replicas))
        self.assertEqual(result.returncode, 2)
        payload = parse_json(result)
        self.assertEqual(payload["error"]["code"], "E_DUPLICATE_REPLICA")
        self.assertFalse(self.state.exists())

    def test_deadline_before_arrival_fails_without_state(self) -> None:
        requests = json.loads(self.requests.read_text(encoding="utf-8"))
        requests["requests"][0]["arrival_ms"] = 10
        requests["requests"][0]["deadline_ms"] = 5
        result = self._load(requests=self._write("bad-deadline.json", requests))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            parse_json(result)["error"]["code"], "E_DEADLINE_BEFORE_ARRIVAL"
        )
        self.assertFalse(self.state.exists())

    def test_sequence_limit_too_small_fails_without_state(self) -> None:
        replicas = json.loads(self.replicas.read_text(encoding="utf-8"))
        replicas["replicas"][0]["max_sequence_length"] = 5
        result = self._load(replicas=self._write("short-sequence.json", replicas))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            parse_json(result)["error"]["code"], "E_SEQUENCE_LIMIT_TOO_SMALL"
        )
        self.assertFalse(self.state.exists())

    def test_bad_fixed_scale_fails_without_state(self) -> None:
        precision = json.loads(self.precision.read_text(encoding="utf-8"))
        precision["compute_scale"] = 0
        result = self._load(precision=self._write("bad-precision.json", precision))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(parse_json(result)["error"]["code"], "E_BAD_FIXED_SCALE")
        self.assertFalse(self.state.exists())

    def test_duplicate_request_sequence_fails_without_state(self) -> None:
        requests = json.loads(self.requests.read_text(encoding="utf-8"))
        requests["requests"].append(dict(requests["requests"][0]))
        result = self._load(requests=self._write("duplicate-requests.json", requests))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            parse_json(result)["error"]["code"], "E_DUPLICATE_REQUEST"
        )
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()

