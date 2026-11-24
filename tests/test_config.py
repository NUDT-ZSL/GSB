from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from relief_tiles.config import parse_config
from relief_tiles.errors import BusinessError
from tests.helpers import cli_environment, config_dict

import os
import subprocess
import sys


class ConfigValidationTests(unittest.TestCase):
    def assert_code(self, mutate, expected_code: str) -> None:
        value = config_dict()
        mutate(value)
        with self.assertRaises(BusinessError) as caught:
            parse_config(value)
        self.assertEqual(caught.exception.code, expected_code)

    def test_invalid_fields_have_stable_business_codes(self) -> None:
        self.assert_code(lambda value: value["canvas"].update(width=0), "E_CANVAS")
        self.assert_code(lambda value: value["grid"].update(rows=0), "E_GRID")
        self.assert_code(lambda value: value["canvas"].update(width=49), "E_GRID")
        self.assert_code(lambda value: value.update(seed=-1), "E_SEED")
        self.assert_code(
            lambda value: value["pattern"].update(octaves=[]), "E_OCTAVE"
        )
        self.assert_code(
            lambda value: value["pattern"]["octaves"][1].update(
                frequency=value["pattern"]["octaves"][0]["frequency"]
            ),
            "E_FREQUENCY",
        )
        self.assert_code(
            lambda value: value["pattern"]["octaves"][0].update(amplitude=0),
            "E_AMPLITUDE",
        )
        self.assert_code(
            lambda value: value["light"].update(direction=[1.0, 2.0, 3.0]),
            "E_LIGHT",
        )
        self.assert_code(
            lambda value: value["camera"].update(distance=0), "E_CAMERA"
        )
        self.assert_code(
            lambda value: value["emboss"].update(strength=0), "E_EMBOSS"
        )
        self.assert_code(
            lambda value: value["emboss"].update(normalization_max=0),
            "E_NORMALIZATION",
        )
        self.assert_code(
            lambda value: value["emboss"].update(quantization_levels=1),
            "E_QUANTIZATION",
        )
        self.assert_code(
            lambda value: value["resources"].update(concurrency_limit=0),
            "E_RESOURCE",
        )
        self.assert_code(
            lambda value: value["resources"].update(memory_budget_pixels=1),
            "E_BUDGET",
        )
        self.assert_code(
            lambda value: value["slice"].update(preview_row=99), "E_BLOCK_RANGE"
        )

    def test_invalid_config_writes_no_state_or_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = config_dict()
            bad["grid"]["rows"] = 0
            config_path = root / "bad.json"
            config_path.write_text(json.dumps(bad), encoding="utf-8")
            state = root / "state"
            output = root / "out"
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "relief_tiles.cli",
                    "render",
                    "--config",
                    str(config_path),
                    "--state",
                    str(state),
                    "--output",
                    str(output),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=cli_environment(),
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(process.returncode, 2)
            payload = json.loads(process.stderr)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "E_GRID")
            self.assertFalse(state.exists())
            self.assertFalse(output.exists())

    def test_normalized_version_is_stable_and_order_independent(self) -> None:
        first = config_dict()
        second = copy.deepcopy(first)
        second = dict(reversed(list(second.items())))
        self.assertEqual(parse_config(first).version, parse_config(second).version)
        second["seed"] += 1
        self.assertNotEqual(parse_config(first).version, parse_config(second).version)


if __name__ == "__main__":
    unittest.main()
