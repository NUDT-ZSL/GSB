from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.helpers import base_task, frame_value, write_case


ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def _run(self, task_path: Path) -> tuple[int, dict[str, object]]:
        completed = subprocess.run(
            [sys.executable, "-m", "hdr_color", str(task_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                f"CLI did not return JSON: {completed.stdout!r} {completed.stderr!r}"
            )
            raise exc
        return completed.returncode, payload

    def test_invalid_transfer_has_business_code_and_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("out")
            task["source"]["transfer"] = "MYSTERY_CURVE"
            task_path = write_case(
                root, task, [frame_value(index) for index in range(4)]
            )
            code, payload = self._run(task_path)
            self.assertEqual(code, 2)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"]["code"], "E_TRANSFER_FUNCTION")
            self.assertFalse((root / "out").exists())

    def test_invalid_gamut_and_luminance_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index) for index in range(3)]
            task = base_task("out-gamut")
            task["target"]["gamut"] = "IMAGINARY"
            code, payload = self._run(write_case(root, task, frames))
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_GAMUT")
            self.assertFalse((root / "out-gamut").exists())

            task = base_task("out-luminance")
            task["luminance"]["target_peak_nits"] = 50.0
            code, payload = self._run(write_case(root, task, frames))
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_LUMINANCE")
            self.assertFalse((root / "out-luminance").exists())

    def test_tone_mapping_domain_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("out-tone")
            task["tone_mapping"]["white_nits"] = 10.0
            code, payload = self._run(
                write_case(root, task, [frame_value(index) for index in range(3)])
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_TONE_MAPPING")
            self.assertFalse((root / "out-tone").exists())

    def test_nan_json_token_is_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("out-nan")
            task_path = write_case(root, task, [frame_value(index) for index in range(3)])
            frames_path = root / "frames.json"
            text = frames_path.read_text(encoding="utf-8")
            text = text.replace("1.25", "NaN", 1)
            frames_path.write_text(text, encoding="utf-8")
            code, payload = self._run(task_path)
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_FRAME_NONFINITE")
            self.assertFalse((root / "out-nan").exists())

    def test_width_mismatch_and_negative_input_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frames = [frame_value(index) for index in range(3)]
            frames[-1] = frames[-1][:-1]
            task = base_task("out-width")
            code, payload = self._run(write_case(root, task, frames))
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_FRAME_WIDTH")
            self.assertFalse((root / "out-width").exists())

            frames = [frame_value(index) for index in range(3)]
            frames[1][0][0] = -0.1
            task = base_task("out-negative")
            code, payload = self._run(write_case(root, task, frames))
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_FRAME_NEGATIVE")
            self.assertFalse((root / "out-negative").exists())

    def test_duplicate_json_keys_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = base_task("out-duplicate")
            task_path = write_case(root, task, [frame_value(index) for index in range(3)])
            text = task_path.read_text(encoding="utf-8")
            text = text.replace(
                '"task_id": "unit-test",',
                '"task_id": "unit-test", "task_id": "duplicate",',
                1,
            )
            task_path.write_text(text, encoding="utf-8")
            code, payload = self._run(task_path)
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_JSON_PARSE")
            self.assertFalse((root / "out-duplicate").exists())


if __name__ == "__main__":
    unittest.main()
