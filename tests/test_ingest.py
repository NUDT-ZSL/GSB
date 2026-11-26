from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from ifdata.config import DEFAULT_CONFIG
from ifdata.errors import E_BAD_FIELD, E_DUP_SAMPLE
from ifdata.stages.ingest import run_ingest
from ifdata.utils import parse_annotation_revision


def sample(sample_id: str, **overrides):
    value = {
        "sample_id": sample_id,
        "instruction": "说明一个本地确定性流程。",
        "input": "固定种子上下文。",
        "output": "先校验输入，再提交快照，并保留审计记录。",
        "source_tag": "wiki",
        "quality_flag": "gold",
        "lang": "zh",
        "annotation_revision": "1.0.0",
    }
    value.update(overrides)
    return value


class IngestTests(unittest.TestCase):
    def test_sorted_duplicate_and_field_errors(self):
        rows = [
            sample("s0003"),
            sample("s0001"),
            sample("s0002"),
            sample("s0002", output=""),
            sample("s0004", annotation_revision="not-a-revision"),
            sample("s0005", instruction="x" * 5000),
            sample("s0006", lang="xx"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.jsonl"
            path.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
                + "\n",
                encoding="utf-8",
            )
            result = run_ingest(path, copy.deepcopy(DEFAULT_CONFIG))
        self.assertEqual(
            [item["sample_id"] for item in result["samples"]],
            ["s0001", "s0003"],
        )
        status_by_id = {
            item["sample_id"]: item for item in result["statuses"]
        }
        self.assertEqual(status_by_id["s0002"]["code"], E_DUP_SAMPLE)
        self.assertEqual(status_by_id["s0004"]["code"], E_BAD_FIELD)
        self.assertEqual(status_by_id["s0005"]["code"], E_BAD_FIELD)
        self.assertEqual(status_by_id["s0006"]["code"], E_BAD_FIELD)
        self.assertEqual(result["summary"]["duplicate_ids"], ["s0002"])
        self.assertTrue(
            all(
                item["status"] == "ok"
                for item in result["statuses"]
                if item["sample_id"] in {"s0001", "s0003"}
            )
        )

    def test_revision_parser_accepts_controlled_forms(self):
        self.assertEqual(parse_annotation_revision(7), 7)
        self.assertEqual(parse_annotation_revision("r12"), 12)
        self.assertEqual(parse_annotation_revision("rev-7"), 7)
        self.assertEqual(parse_annotation_revision("1.2.3"), 1_002_003)
        with self.assertRaises(ValueError):
            parse_annotation_revision(-1)

    def test_malformed_json_is_bad_field_with_line_attribution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "raw.jsonl"
            path.write_text('{"sample_id": "s1"}\n{not-json}\n', encoding="utf-8")
            result = run_ingest(path, copy.deepcopy(DEFAULT_CONFIG))
        self.assertEqual(result["summary"]["valid_rows"], 0)
        self.assertEqual(len(result["statuses"]), 2)
        self.assertTrue(
            all(item["code"] == E_BAD_FIELD for item in result["statuses"])
        )
        self.assertTrue(
            any(
                item["sample_id"] == "__line_000002"
                for item in result["statuses"]
            )
        )


if __name__ == "__main__":
    unittest.main()

