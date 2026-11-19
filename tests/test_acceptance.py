from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from intent_interop.config_io import read_config_document
from intent_interop.demo import run_demo
from intent_interop.io_utils import json_dumps
from intent_interop.pipeline import adjudicate, match_single
from intent_interop.writers import write_config_document

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class AcceptanceTests(unittest.TestCase):
    def test_demo_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report_dir = Path(temp) / "reports"
            summary = run_demo(DATA, report_dir)
            self.assertEqual(summary["status"], "OK")
            self.assertEqual(summary["failed"], 0)
            self.assertGreaterEqual(summary["passed"], 25)
            self.assertTrue((report_dir / "baseline.json").exists())
            self.assertTrue((report_dir / "semantic_loss.json").exists())
            self.assertTrue((report_dir / "demo_summary.json").exists())

    def test_report_is_byte_reproducible(self) -> None:
        first, code = adjudicate(
            samples_path=DATA / "queries.jsonl",
            v1_path=DATA / "config_v1.txt",
            v2_path=DATA / "config_v2.txt",
            v3_path=DATA / "config_v3.json",
        )
        second, second_code = adjudicate(
            samples_path=DATA / "queries.jsonl",
            v1_path=DATA / "config_v1.txt",
            v2_path=DATA / "config_v2.txt",
            v3_path=DATA / "config_v3.json",
        )
        self.assertEqual(code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(json_dumps(first), json_dumps(second))

    def test_same_config_repeated_match_is_byte_reproducible(self) -> None:
        first, _ = match_single(
            samples_path=DATA / "queries.jsonl",
            config_path=DATA / "config_v3.json",
        )
        second, _ = match_single(
            samples_path=DATA / "queries.jsonl",
            config_path=DATA / "config_v3.json",
        )
        self.assertEqual(json_dumps(first), json_dumps(second))

    def test_fixture_sample_fields_are_exact(self) -> None:
        first_line = (DATA / "queries.jsonl").read_text(encoding="utf-8").splitlines()[0]
        row = json.loads(first_line)
        self.assertEqual(
            set(row),
            {"query_id", "query_text", "locale", "label_raw"},
        )

    def test_v1_missing_nondefault_threshold_is_semantic_loss(self) -> None:
        source = read_config_document(DATA / "config_v3.json")
        changed = source.with_changes(
            intents=tuple(
                spec.with_changes(threshold=0.12) for spec in source.intents
            ),
            checksum=None,
        )
        with tempfile.TemporaryDirectory() as temp:
            v3_path = Path(temp) / "config_v3_threshold.json"
            write_config_document(changed, v3_path, version="v3")
            report, code = adjudicate(
                samples_path=DATA / "queries.jsonl",
                v1_path=DATA / "config_v1.txt",
                v2_path=DATA / "config_v2.txt",
                v3_path=v3_path,
            )
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "E_SEMANTIC_LOSS")
        self.assertIn("threshold", report["semantic_loss"]["unrestorable_fields"])
        self.assertIsNone(report["conclusion"])


if __name__ == "__main__":
    unittest.main()
