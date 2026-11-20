from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qxworkbench.errors import WorkbenchError
from qxworkbench.migration import MigrationPolicies, migrate_rules
from qxworkbench.rules import (
    parse_rules,
    parse_rules_text,
    ruleset_checksum,
    serialize_ruleset,
)

from support import prepare_samples


class RuleParsingAndMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.samples = prepare_samples(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_three_formats_parse_deterministically(self) -> None:
        v1 = parse_rules(self.samples / "rules_v1.rules")
        v2 = parse_rules(self.samples / "rules_v2.rules")
        v3 = parse_rules(self.samples / "rules_v3.json")
        self.assertEqual(v1.format_version, "v1")
        self.assertEqual(v2.format_version, "v2")
        self.assertEqual(v3.format_version, "v3")
        self.assertEqual(len(v1.rules), 8)
        self.assertEqual(len(v2.rules), 8)
        self.assertEqual(len(v3.rules), 8)
        self.assertIn("v3", (self.samples / "rules_v3.json").read_text(encoding="utf-8"))

        first = serialize_ruleset(v1)
        second = serialize_ruleset(parse_rules(self.samples / "rules_v1.rules"))
        self.assertEqual(first, second)
        self.assertEqual(ruleset_checksum(v1), ruleset_checksum(parse_rules(self.samples / "rules_v1.rules")))
        self.assertEqual(
            first,
            serialize_ruleset(parse_rules_text(first.decode("utf-8"), source="<v1>")),
        )
        for ruleset, path in (
            (v2, self.samples / "rules_v2.rules"),
            (v3, self.samples / "rules_v3.json"),
        ):
            canonical = serialize_ruleset(ruleset)
            self.assertTrue(canonical.endswith(b"\n"))
            self.assertEqual(
                canonical,
                serialize_ruleset(parse_rules(path)),
            )

    def test_unknown_version_code(self) -> None:
        with self.assertRaises(WorkbenchError) as caught:
            parse_rules_text("# version=v9\nr1|zh|汽车|en|car\n", source="bad.rules")
        self.assertEqual(caught.exception.code, "E_VERSION_UNSUPPORTED")
        self.assertEqual(caught.exception.details["version"], "v9")

    def test_format_corruption_has_source_line(self) -> None:
        cases = [
            "# version=v1\nr1|zh|汽车|en\n",
            "# version=v2\nrule_id=r1|source_lang=zh|source_term=汽车|target_lang=en\n",
            "# version=v2\nrule_id=r1|source_lang=zh|source_term=汽车|target_lang=en|target_term=car|bridge_confidence_weight=NaN\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(WorkbenchError) as caught:
                    parse_rules_text(text, source="bad.rules")
                self.assertEqual(caught.exception.code, "E_FORMAT_CORRUPT")
                self.assertEqual(caught.exception.details["location"]["line"], 2)
                self.assertEqual(caught.exception.details["location"]["source"], "bad.rules")

    def test_v1_to_v2_completes_defaults_and_records_sources(self) -> None:
        source = parse_rules(self.samples / "rules_v1.rules")
        migrated = migrate_rules(source, "v2")
        self.assertEqual(migrated.ruleset.format_version, "v2")
        self.assertEqual(migrated.report["rule_count_in"], 8)
        self.assertEqual(migrated.report["rule_count_out"], 8)
        self.assertEqual(len(migrated.report["completions"]), 24)
        self.assertTrue(all(rule.bridge_confidence_weight is not None for rule in migrated.ruleset.rules))
        self.assertTrue(all(rule.max_expansion_depth is not None for rule in migrated.ruleset.rules))
        self.assertTrue(all(rule.enabled is True for rule in migrated.ruleset.rules))
        reread = migrated.reread()
        self.assertEqual(reread.format_version, "v2")
        self.assertEqual(ruleset_checksum(reread), ruleset_checksum(migrated.ruleset))

        back = migrate_rules(migrated.ruleset, "v1")
        self.assertEqual(serialize_ruleset(back.ruleset), serialize_ruleset(source))

    def test_v2_to_v3_round_trip_on_expressible_fields(self) -> None:
        source = parse_rules(self.samples / "rules_v2.rules")
        upgraded = migrate_rules(source, "v3")
        self.assertEqual(len(upgraded.report["completions"]), 0)
        self.assertEqual(upgraded.reread().format_version, "v3")
        downgraded = migrate_rules(upgraded.ruleset, "v2")
        self.assertEqual(
            serialize_ruleset(downgraded.ruleset),
            serialize_ruleset(source),
        )
        self.assertTrue(
            any(
                item["kind"] == "condition"
                for item in downgraded.report["flattened"]
            )
        )

    def test_v3_downgrade_flattens_targets_and_drops_nested_conditions(self) -> None:
        source = parse_rules(self.samples / "rules_v3.json")
        migrated = migrate_rules(
            source,
            "v2",
            policies=MigrationPolicies(multi_target="flatten", condition="flatten"),
        )
        self.assertGreater(len(migrated.ruleset.rules), len(source.rules))
        self.assertTrue(migrated.report["flattened"])
        self.assertTrue(migrated.report["drops"])
        drop_kinds = {item["kind"] for item in migrated.report["drops"]}
        self.assertIn("condition", drop_kinds)
        self.assertEqual(migrated.reread().format_version, "v2")

        to_v1 = migrate_rules(
            source,
            "v1",
            policies=MigrationPolicies(multi_target="flatten", condition="drop"),
        )
        self.assertEqual(to_v1.reread().format_version, "v1")
        self.assertGreaterEqual(len(to_v1.report["drops"]), 1)
        self.assertTrue(
            any(item["kind"] == "optional_field" for item in to_v1.report["drops"])
        )

    def test_multi_target_drop_policy_is_explicit(self) -> None:
        source = parse_rules(self.samples / "rules_v3.json")
        migrated = migrate_rules(
            source,
            "v2",
            policies=MigrationPolicies(multi_target="drop", condition="drop"),
        )
        multi_drops = [
            item for item in migrated.report["drops"] if item["kind"] == "multi_target"
        ]
        self.assertTrue(multi_drops)
        self.assertTrue(all(len(rule.targets) == 1 for rule in migrated.ruleset.rules))


if __name__ == "__main__":
    unittest.main()
