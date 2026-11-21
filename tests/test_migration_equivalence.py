import tempfile
import unittest
from pathlib import Path

from diagwb.equivalence import adjudicate, round_trip
from diagwb.errors import DiagError
from diagwb.formats import parse_file, parse_text
from diagwb.migration import (MigrationPolicy, migrate, normalize_record)
from tests._helpers import write_triplet


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.result_dir = write_triplet(self.dir, "tool_a",
                                        "tool_a@logical")

    def tearDown(self):
        self.tmp.cleanup()

    def nodes(self):
        return {
            ver: normalize_record(
                parse_file(self.result_dir / f"v{ver}.txt")[1][0])
            for ver in (1, 2, 3)
        }

    def test_v1_upgrade_records_completions(self):
        node = self.nodes()[1]
        names = {item.field_name for item in node.completions}
        self.assertIn("result_schema_version", names)
        self.assertIn("confidence", names)
        self.assertIn("lineage", names)
        self.assertTrue(all(item.action == "synthesized"
                            for item in node.completions))
        self.assertIsNotNone(node.confidence)

    def test_v3_downgrade_emits_discard_audit(self):
        outcome = migrate(self.nodes()[3], 1, MigrationPolicy())
        kinds = {item.item_type for item in outcome.discards}
        self.assertIn("shard_ref", kinds)
        self.assertIn("lineage", kinds)
        self.assertIn("aggregation", kinds)
        self.assertTrue(all(item.rule in ("flatten", "drop", "keep")
                            for item in outcome.discards))
        self.assertTrue(any(item.rule == "flatten"
                            for item in outcome.discards))

    def test_round_trip_all_versions(self):
        v3 = self.nodes()[3]
        for version in (1, 2, 3):
            verdict = round_trip(v3, version)
            self.assertTrue(verdict.ok, verdict)
            self.assertTrue(verdict.payload_equal)
            self.assertTrue(verdict.status_equal)
            self.assertTrue(verdict.shards_equal)

    def test_native_tri_format_equivalence(self):
        verdict = adjudicate(self.nodes())
        self.assertTrue(verdict.equivalent)
        self.assertIn("confidence", verdict.irrecoverable)

    def test_drop_policy_causes_semantic_drift(self):
        v3 = self.nodes()[3]
        outcome = migrate(v3, 1, MigrationPolicy(
            shards="drop", aggregation="drop", lineage="drop"))
        rebuilt = normalize_record(
            parse_text(f"# FORMAT_VERSION=1\n{outcome.body}\n")[1][0])
        with self.assertRaises(DiagError) as caught:
            adjudicate({1: rebuilt, 3: v3})
        self.assertEqual(caught.exception.code, "E_SEMANTIC_DRIFT")
        self.assertIn("shards",
                      caught.exception.context["irrecoverable"])

    def test_v2_v3_confidence_and_lineage_precision(self):
        nodes = self.nodes()
        v2_rt = round_trip(nodes[3], 2)
        self.assertTrue(v2_rt.confidence_equal)
        self.assertTrue(v2_rt.lineage_equal)


if __name__ == "__main__":
    unittest.main()
