import json
import os
import tempfile
import unittest

from layout_order.errors import E_DATA_INVALID, LayoutOrderError
from layout_order.generator import (
    FAMILIES,
    generate_page,
    write_jsonl,
    baseline_specs,
    scale_sweep_specs,
)
from layout_order.geometry import (
    SPANNING_WIDTH_RATIO,
    load_jsonl,
    normalize_page,
    parse_page,
)


class GeneratorTests(unittest.TestCase):
    def test_deterministic_byte_identical_records(self) -> None:
        first = generate_page("two_column", 18, 2024, "page_x")
        second = generate_page("two_column", 18, 2024, "page_x")
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

    def test_all_families_valid_and_truth_is_permutation(self) -> None:
        for family in FAMILIES:
            record = generate_page(family, 14, 123, f"p_{family}")
            page = parse_page(record)
            indices = sorted(block.reading_index for block in page.blocks)
            self.assertEqual(indices, list(range(len(page.blocks))))
            for block in page.blocks:
                self.assertTrue(0 <= block.x1 < block.x2 <= page.canvas_width)
                self.assertTrue(0 <= block.y1 < block.y2 <= page.canvas_height)

    def test_jsonl_roundtrip_and_dataset_specs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "samples.jsonl")
            records = write_jsonl(baseline_specs(), path)
            pages = load_jsonl(path)
            self.assertEqual(len(records), len(pages))
            self.assertEqual(len(pages), len(baseline_specs()))
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line for line in handle.read().splitlines() if line]
            self.assertEqual(len(lines), len(records))

    def test_scale_sweep_sizes_are_strictly_growing(self) -> None:
        sizes = [spec["n_blocks"] for spec in scale_sweep_specs()]
        self.assertEqual(sizes, sorted(sizes))
        self.assertGreaterEqual(len(sizes), 7)

    def test_invalid_rectangle_rejected(self) -> None:
        record = generate_page("single_column", 6, 1, "bad")
        record["blocks"][0]["x2"] = record["blocks"][0]["x1"]
        with self.assertRaises(LayoutOrderError) as caught:
            parse_page(record)
        self.assertEqual(caught.exception.code, E_DATA_INVALID)


class GeometryTests(unittest.TestCase):
    def test_normalization_features_and_left_to_right_columns(self) -> None:
        record = generate_page("two_column", 16, 77, "geo")
        page = parse_page(record)
        features = normalize_page(page)
        self.assertEqual([f.block_id for f in features], sorted(f.block_id for f in features))
        columns = sorted({f.column for f in features if f.column >= 0})
        self.assertEqual(columns, [0, 1])
        centers = {
            column: min(f.cx for f in features if f.column == column)
            for column in columns
        }
        self.assertLess(centers[0], centers[1])
        for feature in features:
            for value in (feature.nx1, feature.ny1, feature.nx2, feature.ny2):
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 1.0)

    def test_spanning_title_detected_and_caption_kept_in_body(self) -> None:
        record = generate_page("mixed_regions", 20, 88, "mix")
        page = parse_page(record)
        features = normalize_page(page)
        titles = [f for f in features if f.block_type == "title"]
        self.assertTrue(titles)
        self.assertTrue(all(f.is_spanning for f in titles))
        captions = [f for f in features if f.block_type == "caption"]
        self.assertTrue(captions)
        self.assertTrue(all(f.region == "body" for f in captions))

    def test_deterministic_features_across_calls(self) -> None:
        record = generate_page("three_column", 21, 99, "det")
        page = parse_page(record)
        first = [f.to_dict() for f in normalize_page(page)]
        second = [f.to_dict() for f in normalize_page(page)]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
