from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from anim_replay.cli import main
from anim_replay.errors import SchemaError
from anim_replay.schema import validate_input

from tests.helpers import sample_input_data


class SchemaValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = sample_input_data()

    def _invalid(self, mutate) -> None:
        data = copy.deepcopy(self.data)
        mutate(data)
        with self.assertRaises(SchemaError) as context:
            validate_input(data)
        self.assertEqual(context.exception.code, "E_SCHEMA")

    def test_duplicate_bone(self) -> None:
        def mutate(data):
            data["skeleton"]["bones"][1]["name"] = data["skeleton"]["bones"][0]["name"]

        self._invalid(mutate)

    def test_parent_cycle(self) -> None:
        def mutate(data):
            root = next(item for item in data["skeleton"]["bones"] if item["name"] == "root")
            root["parent"] = "hips"

        self._invalid(mutate)

    def test_non_finite_rest_translation(self) -> None:
        def mutate(data):
            data["skeleton"]["bones"][0]["rest"]["translation"][0] = float("nan")

        self._invalid(mutate)

    def test_keyframe_times_strictly_increase(self) -> None:
        def mutate(data):
            keys = data["clips"][0]["tracks"]["hips"]["rotation"]
            keys[1]["t"] = keys[0]["t"]

        self._invalid(mutate)

    def test_keyframe_value_finite(self) -> None:
        def mutate(data):
            data["clips"][0]["tracks"]["hips"]["rotation"][0]["value"][0] = float("inf")

        self._invalid(mutate)

    def test_transition_parameter_must_exist(self) -> None:
        def mutate(data):
            data["state_machine"]["transitions"][0]["when"][0]["param"] = "missing_parameter"

        self._invalid(mutate)

    def test_timeline_alignment(self) -> None:
        def mutate(data):
            data["timeline"]["frame_step_ms"] = 17

        self._invalid(mutate)

    def test_no_output_files_on_schema_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = copy.deepcopy(self.data)
            bad["skeleton"]["bones"][0]["rest"]["translation"][0] = float("nan")
            input_path = root / "bad.json"
            input_path.write_text(json.dumps(bad), encoding="utf-8")
            state_dir = root / "state"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main(
                    [
                        "replay",
                        "--input",
                        str(input_path),
                        "--state-dir",
                        str(state_dir),
                    ]
                )
            value = json.loads(stdout.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(value["error"]["code"], "E_SCHEMA")
            self.assertFalse(state_dir.exists())
            self.assertEqual(sorted(item.name for item in root.iterdir()), ["bad.json"])


if __name__ == "__main__":
    unittest.main()

