from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ticket_risk.loader import load_inputs


ROOT = Path(__file__).resolve().parents[1]


class LoaderTests(unittest.TestCase):
    def test_invalid_samples_keep_valid_records_and_failure_codes(self) -> None:
        bundle = load_inputs(
            ROOT / "tests/fixtures/invalid_samples.jsonl",
            ROOT / "resources/rules.json",
            ROOT / "config/determination.json",
        )
        self.assertFalse(bundle.fatal)
        self.assertEqual([sample.sample_id for sample in bundle.samples], ["OK01", "OK02"])
        codes = {failure.business_code for failure in bundle.failures}
        self.assertEqual(codes, {"E_BAD_RECORD", "E_EMPTY_TEXT", "E_UNKNOWN_CHANNEL"})
        self.assertEqual(
            [failure.to_dict() for failure in bundle.failures],
            [failure.to_dict() for failure in sorted(bundle.failures, key=lambda item: item.sort_key())],
        )

    def test_rule_cycle_is_fatal(self) -> None:
        bundle = load_inputs(
            ROOT / "data/samples.jsonl",
            ROOT / "tests/fixtures/cycle_rules.json",
            ROOT / "config/determination.json",
        )
        self.assertTrue(bundle.fatal)
        self.assertIn("E_RULE_CYCLE", {failure.business_code for failure in bundle.failures})

    def test_unknown_keyword_level_mapping_is_rule_cycle_code(self) -> None:
        rules = json.loads((ROOT / "resources/rules.json").read_text(encoding="utf-8"))
        rules["risk_keywords"][0]["level"] = "未知等级"
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "rules.json"
            rules_path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
            bundle = load_inputs(
                ROOT / "data/samples.jsonl",
                rules_path,
                ROOT / "config/determination.json",
            )
        self.assertTrue(bundle.fatal)
        self.assertIn("E_RULE_CYCLE", {failure.business_code for failure in bundle.failures})

    def test_non_finite_sentiment_weight_is_rejected(self) -> None:
        rules = json.loads((ROOT / "resources/rules.json").read_text(encoding="utf-8"))
        rules["sentiment_lexicon"][0]["weight"] = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "rules.json"
            rules_path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
            bundle = load_inputs(
                ROOT / "data/samples.jsonl",
                rules_path,
                ROOT / "config/determination.json",
            )
        self.assertTrue(bundle.fatal)
        self.assertIn("E_BAD_RECORD", {failure.business_code for failure in bundle.failures})

    def test_fingerprints_are_semantic_and_stable(self) -> None:
        first = load_inputs(
            ROOT / "data/samples.jsonl",
            ROOT / "resources/rules.json",
            ROOT / "config/determination.json",
        )
        with tempfile.TemporaryDirectory() as directory:
            rules = json.loads((ROOT / "resources/rules.json").read_text(encoding="utf-8"))
            config = json.loads((ROOT / "config/determination.json").read_text(encoding="utf-8"))
            rules_path = Path(directory) / "rules.json"
            config_path = Path(directory) / "config.json"
            # Different key order and whitespace must not change semantic hashes.
            rules_path.write_text(
                json.dumps(rules, ensure_ascii=False, sort_keys=False, indent=4),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, sort_keys=False, indent=4),
                encoding="utf-8",
            )
            second = load_inputs(ROOT / "data/samples.jsonl", rules_path, config_path)
        self.assertEqual(first.config_hash, second.config_hash)
        self.assertEqual(first.resource_fingerprint, second.resource_fingerprint)


if __name__ == "__main__":
    unittest.main()
