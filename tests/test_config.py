from __future__ import annotations

import copy
import unittest

from edgecache.config import ContentState, Policy
from edgecache.errors import CycleError, PolicyError

from tests.helpers import load_doc


class ConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content_doc = load_doc("content.json")
        self.policy_doc = load_doc("policy.json")
        self.content = ContentState.from_doc(self.content_doc)

    def test_valid_policy_and_content(self) -> None:
        policy = Policy.from_doc(self.policy_doc, self.content.paths)
        self.assertEqual(policy.policy_version, "policy-v1")
        self.assertEqual(policy.node_capacity, 4)
        self.assertEqual(len(self.content.paths), 5)
        self.assertIn("/assets/app.css", self.content.effective)
        self.assertEqual(
            self.content.sources["/assets/app.css"],
            ("/assets/base.css", "/assets/theme.css"),
        )

    def test_duplicate_key_fields_rejected(self) -> None:
        doc = copy.deepcopy(self.policy_doc)
        doc["key_fields"] = ["path", "tenant", "tenant"]
        with self.assertRaises(PolicyError) as caught:
            Policy.from_doc(doc, self.content.paths)
        self.assertEqual(caught.exception.code, "E_POLICY")
        self.assertIn("key_fields[2]", caught.exception.location or "")

    def test_non_positive_ttl_rejected(self) -> None:
        doc = copy.deepcopy(self.policy_doc)
        doc["ttl_seconds_by_path"]["/index.html"] = 0
        with self.assertRaises(PolicyError) as caught:
            Policy.from_doc(doc, self.content.paths)
        self.assertEqual(caught.exception.code, "E_POLICY")
        self.assertIn("/index.html", caught.exception.location or "")

    def test_missing_derived_reference_rejected(self) -> None:
        doc = copy.deepcopy(self.content_doc)
        doc["contents"][3]["derived"]["sources"].append("/missing.css")
        with self.assertRaises(PolicyError) as caught:
            ContentState.from_doc(doc)
        self.assertEqual(caught.exception.code, "E_POLICY")
        self.assertIn("derived.sources", caught.exception.location or "")

    def test_cycle_has_specific_code_and_location(self) -> None:
        with self.assertRaises(CycleError) as caught:
            ContentState.from_doc(load_doc("content_cycle.json"))
        self.assertEqual(caught.exception.code, "E_CYCLE")
        self.assertIn("contents[3]", caught.exception.location or "")

    def test_declared_derived_etag_must_match_computed_value(self) -> None:
        doc = copy.deepcopy(self.content_doc)
        doc["contents"][3]["etag"] = '"sha256-wrong"'
        with self.assertRaises(PolicyError) as caught:
            ContentState.from_doc(doc)
        self.assertEqual(caught.exception.code, "E_POLICY")
        self.assertIn("contents[3].etag", caught.exception.location or "")

    def test_evolution_rewrites_derived_etag_declarations(self) -> None:
        evolved = self.content.with_evolved_base("/assets/base.css", '"base-v2"', "changed\n")
        document_paths = {item["path"]: item for item in evolved.raw["contents"]}
        for path, value in evolved.effective.items():
            if value.sources:
                self.assertEqual(document_paths[path]["etag"], value.etag)

    def test_propagation_order_is_stable_and_topological(self) -> None:
        expected = (
            "/assets/base.css",
            "/assets/app.css",
            "/assets/app.manifest.json",
        )
        order_a = self.content.descendants("/assets/base.css")
        reversed_doc = copy.deepcopy(self.content_doc)
        reversed_doc["contents"] = list(reversed(reversed_doc["contents"]))
        order_b = ContentState.from_doc(reversed_doc).descendants("/assets/base.css")
        self.assertEqual(order_a, expected)
        self.assertEqual(order_b, expected)

    def test_policy_requires_ttl_for_every_path(self) -> None:
        doc = copy.deepcopy(self.policy_doc)
        del doc["ttl_seconds_by_path"]["/assets/theme.css"]
        with self.assertRaises(PolicyError) as caught:
            Policy.from_doc(doc, self.content.paths)
        self.assertEqual(caught.exception.code, "E_POLICY")
        self.assertIn("theme.css", str(caught.exception.details))


if __name__ == "__main__":
    unittest.main()
