from __future__ import annotations

import copy
import json
import unittest

from annotation_app.config import Limits
from annotation_app.consistency import cross_check
from annotation_app.errors import AppError, ErrorCode
from annotation_app.ontology import (
    OntologySnapshot,
    OntologyStore,
    make_ontology_dict,
)
from annotation_app.reader import parse_document
from annotation_app.resources import enforce_limits

from tests.helpers import ROOT, load_v2, ontology_v1, ontology_v2, sample_v2_dict


class OntologyResourceTests(unittest.TestCase):
    def test_checksum_tampering_is_rejected(self):
        value = json.loads(
            (ROOT / "snapshots" / "ontology-v1.json").read_text("utf-8")
        )
        value["argument_roles"].append("shadow-role")
        with self.assertRaises(AppError) as caught:
            OntologySnapshot.from_dict(value)
        self.assertEqual(caught.exception.code, ErrorCode.ONTOLOGY_CONTRACT)

    def test_strict_shrink_and_lenient_replacement(self):
        store = OntologyStore(contract_mode="strict")
        store.activate(ontology_v1())
        with self.assertRaises(AppError) as caught:
            store.activate(ontology_v2(), sample=load_v2(), mode="strict")
        self.assertEqual(caught.exception.code, ErrorCode.ONTOLOGY_CONTRACT)
        self.assertEqual(caught.exception.details["affected_count"], 3)
        self.assertEqual(store.active.version, 1)
        activation = store.activate(
            ontology_v2(), sample=load_v2(), mode="lenient"
        )
        self.assertTrue(activation.changed)
        self.assertEqual(store.active.version, 2)
        self.assertEqual(len(activation.affected), 3)

    def test_component_change_requires_version_bump(self):
        store = OntologyStore(contract_mode="lenient")
        store.activate(ontology_v1())
        changed = make_ontology_dict(
            version=2,
            entity_types=["PERSON", "ORG"],
            argument_roles=["employee", "employer"],
            relation_types=["WORKS_AT"],
            migration_policy={"v1_to_v2": {}, "v2_to_v1": {}},
            component_versions={
                "entity_types": 1,
                "argument_roles": 1,
                "relation_types": 1,
                "migration_policy": 1,
            },
        )
        with self.assertRaises(AppError) as caught:
            store.activate(OntologySnapshot.from_dict(changed))
        self.assertEqual(caught.exception.code, ErrorCode.ONTOLOGY_CONTRACT)
        self.assertIn("without a version increase", caught.exception.message)

    def test_import_byte_limit(self):
        with self.assertRaises(AppError) as caught:
            parse_document(b'{"version":"v1"}', max_import_bytes=4)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertEqual(caught.exception.location.stage, "import")

    def test_annotation_limit_reject_and_entities_only_degrade(self):
        sample = load_v2()
        reject_limits = Limits(
            max_import_bytes=1024,
            max_annotation_total=1,
            max_graph_nodes=100,
            on_limit="reject",
        )
        with self.assertRaises(AppError) as caught:
            enforce_limits(sample, reject_limits, stage="test")
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_LIMIT)
        degrade_limits = Limits(
            max_import_bytes=1024,
            max_annotation_total=1,
            max_graph_nodes=100,
            on_limit="entities_only",
        )
        result = enforce_limits(sample, degrade_limits, stage="test")
        self.assertEqual(result.report["status"], "degraded")
        self.assertTrue(
            all(
                node.kind == "entity"
                for document in result.sample.documents
                for node in document.nodes
            )
        )
        self.assertTrue(all(not document.edges for document in result.sample.documents))
        self.assertEqual(result.sample.coref, [])
        self.assertEqual(
            result.sample.metadata["degradation"]["category"], "entities_only"
        )

    def test_graph_node_limit(self):
        limits = Limits(
            max_import_bytes=1024,
            max_annotation_total=100,
            max_graph_nodes=2,
            on_limit="reject",
        )
        with self.assertRaises(AppError) as caught:
            enforce_limits(load_v2(), limits, stage="test")
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertEqual(
            caught.exception.details["violations"][0]["category"], "graph_nodes"
        )

    def test_cross_check_reports_first_inconsistency(self):
        reference = load_v2()
        candidate = load_v2()
        next(
            edge for edge in candidate.documents[0].edges if edge.id == "a1"
        ).role = "employer"
        with self.assertRaises(AppError) as caught:
            cross_check(
                reference,
                {"line_roundtrip": candidate},
                ontology=ontology_v1(),
            )
        self.assertEqual(caught.exception.code, ErrorCode.INCONSISTENT)
        self.assertEqual(caught.exception.details["category"], "arguments")
        self.assertIn("a1", caught.exception.location.field_path)


if __name__ == "__main__":
    unittest.main()

