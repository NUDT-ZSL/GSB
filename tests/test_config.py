from __future__ import annotations

import unittest

from voice_dialog.config import normalize_config, normalize_injections
from voice_dialog.errors import ConfigError

from tests.helpers import TempCase


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = TempCase()

    def tearDown(self) -> None:
        self.case.close()

    def test_invalid_granularity_business_code(self) -> None:
        raw = {
            "emotion_granularity": "seven_way",
        }
        with self.assertRaises(ConfigError) as context:
            normalize_config(raw)
        self.assertEqual(context.exception.code, "E_INVALID_GRANULARITY")

    def test_injection_hash_is_order_independent_and_unique(self) -> None:
        items = [
            {"id": "B", "kind": "zero_duration", "target": "b.wav"},
            {"id": "A", "kind": "nonfinite_sample", "target": "a.wav"},
        ]
        first = normalize_injections({"injections": items})
        second = normalize_injections({"injections": list(reversed(items))})
        self.assertEqual(first["injection_fingerprint"], second["injection_fingerprint"])
        self.assertEqual([item["id"] for item in first["injections"]], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
