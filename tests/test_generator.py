from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from metering_store.generator import generate_dataset, write_dataset
from metering_store.models import parse_record_stream


class GeneratorTests(unittest.TestCase):
    def test_same_seed_is_byte_identical(self) -> None:
        first = generate_dataset(42)
        second = generate_dataset(42)
        self.assertEqual(first.all_base_records, second.all_base_records)
        self.assertEqual(first.manifest(), second.manifest())
        for file_id in first.files:
            self.assertEqual(first.files[file_id].data, second.files[file_id].data)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(first, root / "a")
            write_dataset(second, root / "b")
            first_files = sorted(
                path.relative_to(root / "a")
                for path in (root / "a").rglob("*")
                if path.is_file()
            )
            second_files = sorted(
                path.relative_to(root / "b")
                for path in (root / "b").rglob("*")
                if path.is_file()
            )
            self.assertEqual(first_files, second_files)
            for relative in first_files:
                self.assertEqual(
                    (root / "a" / relative).read_bytes(),
                    (root / "b" / relative).read_bytes(),
                    str(relative),
                )

    def test_event_stream_contains_all_required_kinds(self) -> None:
        dataset = generate_dataset(42)
        kinds = [event.kind for event in dataset.events]
        self.assertIn("write", kinds)
        self.assertIn("duplicate", kinds)
        self.assertIn("near_duplicate", kinds)
        self.assertIn("switch_policy", kinds)
        self.assertIn("delete", kinds)
        self.assertEqual([event.seq for event in dataset.events], list(range(1, 22)))
        self.assertEqual(len(dataset.areas), 3)
        for area in dataset.areas:
            self.assertEqual(len(area.meters), 4)
            self.assertEqual(area.collection_interval_ms, 1_000)
            self.assertEqual(area.batch_window_records, 4)

    def test_exact_and_near_duplicate_contents(self) -> None:
        dataset = generate_dataset(42)
        self.assertEqual(dataset.files["f-0001"].data, dataset.files["f-0002"].data)

        source = [
            item.record for item in parse_record_stream(dataset.files["f-0003"].data)
        ]
        near = [
            item.record for item in parse_record_stream(dataset.files["f-0004"].data)
        ]
        self.assertEqual(len(source), len(near))
        differences = []
        for record_index, (left, right) in enumerate(zip(source, near)):
            changed = {
                key
                for key in left
                if left[key] != right[key]
            }
            if changed:
                differences.append((record_index, changed))
        self.assertEqual(len(differences), 1)
        self.assertEqual(differences[0][0], 7)
        self.assertEqual(differences[0][1], {"quality_flag"})

    def test_manifest_is_canonical_json(self) -> None:
        dataset = generate_dataset(5)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(dataset, root)
            text = (root / "manifest.json").read_text(encoding="utf-8")
            expected = (
                json.dumps(
                    dataset.manifest(),
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            self.assertEqual(text, expected)
            self.assertTrue((root / "records.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
