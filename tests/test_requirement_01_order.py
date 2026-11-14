"""Requirement 1: export order follows the description array, never filenames."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frame_export.batch import build_batch  # noqa: E402
from frame_export.spec import parse_spec  # noqa: E402
from tests.helpers import copy_sample, load_json  # noqa: E402


class DescriptionOrderTests(unittest.TestCase):
    def test_clean_sample_export_order_matches_description(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            batch = build_batch(parse_spec(spec_path))
            described = [frame["file"] for frame in load_json(spec_path)["frames"]]
            exported = [frame.spec.file_ref for frame in batch.frames]
            self.assertEqual(described, exported)
            # Filename sort order must differ from the description order,
            # otherwise this test would not prove anything.
            self.assertNotEqual(sorted(described), described)

    def test_renaming_files_does_not_change_order(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            spec_path = copy_sample(Path(tmp), "clean_storyboard")
            original = build_batch(parse_spec(spec_path))
            original_order = [frame.spec.file_ref for frame in original.frames]

            # Rename every frame file to a name whose lexical order is reversed.
            frames_dir = spec_path.parent / "frames"
            files = sorted(frames_dir.iterdir())
            mapping = {}
            for position, path in enumerate(files):
                new_name = f"z{len(files) - position:03d}_renamed.png"
                mapping[path.name] = new_name
                path.rename(frames_dir / new_name)
            payload = load_json(spec_path)
            for frame in payload["frames"]:
                old = frame["file"].split("/", 1)[1]
                frame["file"] = f"frames/{mapping[old]}"
            spec_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            renamed = build_batch(parse_spec(spec_path))
            renamed_order = [frame.spec.file_ref for frame in renamed.frames]
            self.assertEqual(
                [mapping[name.split("/", 1)[1]] for name in original_order],
                [name.split("/", 1)[1] for name in renamed_order],
            )
            # Pixel content is also still in the described order.
            for before, after in zip(original.frames, renamed.frames):
                self.assertEqual(before.png_bytes, after.png_bytes)


if __name__ == "__main__":
    unittest.main()
