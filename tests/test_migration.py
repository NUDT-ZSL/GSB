from __future__ import annotations

import copy
import unittest

from annotation_app.errors import AppError, ErrorCode
from annotation_app.migration import downgrade_v2_to_v1, upgrade_v1_to_v2
from annotation_app.model import graph_snapshot

from tests.helpers import load_v1, load_v2, ontology_v1


class MigrationTests(unittest.TestCase):
    def test_v1_roundtrip_preserves_event_graph(self):
        v1 = load_v1()
        up = upgrade_v1_to_v2(v1, ontology_v1())
        down = downgrade_v2_to_v1(up.sample, ontology_v1())
        again = upgrade_v1_to_v2(down.sample, ontology_v1())
        self.assertEqual(graph_snapshot(up.sample), graph_snapshot(again.sample))

    def test_v2_downgrade_registers_drops_and_conflicts(self):
        v2 = load_v2()
        result = downgrade_v2_to_v1(v2, ontology_v1())
        categories = {item["category"] for item in result.report.dropped}
        self.assertIn("coref", categories)
        self.assertIn("canonical_id", categories)
        self.assertIn("node_confidence", categories)
        self.assertTrue(
            any(
                item["category"] == "event_multi_trigger"
                for item in result.report.conflicts
            )
        )
        self.assertGreater(len(result.report.differences), 0)
        self.assertTrue(result.report.differences)
        self.assertTrue(any(item["category"] == "events" for item in result.report.differences))

    def test_v1_role_mapping_policy(self):
        value = load_v1()
        value.documents[0].arguments[0].role = "worker"
        result = upgrade_v1_to_v2(value, ontology_v1())
        argument = next(
            edge
            for edge in result.sample.documents[0].edges
            if edge.id == value.documents[0].arguments[0].id
        )
        self.assertEqual(argument.role, "employee")
        self.assertTrue(
            any(item["field"] == "role" for item in result.report.synthesized)
        )

    def test_unknown_v1_role_is_attributed(self):
        value = load_v1()
        value.documents[0].arguments[0].role = "unmapped-role"
        with self.assertRaises(AppError) as caught:
            upgrade_v1_to_v2(value, ontology_v1())
        error = caught.exception
        self.assertEqual(error.code, ErrorCode.ROLE_INVALID)
        self.assertEqual(error.location.stage, "migration")
        self.assertEqual(error.location.version, "v1")
        self.assertIn(".arguments[0].role", error.location.field_path)

    def test_v1_upgrade_synthesizes_event_ids(self):
        result = upgrade_v1_to_v2(load_v1(), ontology_v1())
        triggers = [
            node
            for node in result.sample.documents[0].nodes
            if node.kind == "trigger"
        ]
        self.assertTrue(all(node.event_id for node in triggers))
        self.assertEqual(len(triggers), 2)


if __name__ == "__main__":
    unittest.main()

