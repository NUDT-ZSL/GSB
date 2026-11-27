from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from build_routing.generator import generate_artifacts
from build_routing.normalize import (
    ReferenceValidationError,
    load_project,
    normalize_project,
    reference_report,
)


class NormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        generate_artifacts(cls.root, seed=20261128)
        cls.v1 = load_project(cls.root / "project_v1.json")
        cls.v2 = load_project(cls.root / "project_v2.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_dual_schema_normalizes_to_same_route_semantics_except_injections(self) -> None:
        self.assertEqual(self.v1.project_id, self.v2.project_id)
        self.assertEqual(self.v1.schema_version, 1)
        self.assertEqual(self.v2.schema_version, 2)
        self.assertFalse(self.v1.reference_issues)
        self.assertFalse(self.v2.reference_issues)

        v1_prefixes = self.v1.prefix_by_id()
        v2_prefixes = self.v2.prefix_by_id()
        self.assertEqual(set(v1_prefixes), set(v2_prefixes))
        for prefix_id in v1_prefixes:
            left = v1_prefixes[prefix_id]
            right = v2_prefixes[prefix_id]
            self.assertEqual(left.build_node_id, right.build_node_id)
            self.assertEqual(left.cidr, right.cidr)
            self.assertEqual(len(left.candidates), len(right.candidates))
            left_candidates = {item.id: item for item in left.candidates}
            right_candidates = {item.id: item for item in right.candidates}
            self.assertEqual(set(left_candidates), set(right_candidates))
            for candidate_id in left_candidates:
                lc = left_candidates[candidate_id]
                rc = right_candidates[candidate_id]
                self.assertEqual(lc.next_hop_id, rc.next_hop_id)
                self.assertEqual(lc.as_path, rc.as_path)
                if not (
                    prefix_id == "pfx-bj-maven"
                    and candidate_id == "cand-bj-maven-2"
                ):
                    self.assertEqual(lc.communities, rc.communities)
                if not (
                    prefix_id == "pfx-sh-pypi"
                    and candidate_id == "cand-sh-pypi-1"
                ):
                    self.assertEqual(lc.local_pref, rc.local_pref)

    def test_percent_and_bitmap_conversion(self) -> None:
        v2 = self.v2.prefix_by_id()["pfx-bj-maven"]
        candidate = next(item for item in v2.candidates if item.id == "cand-bj-maven-1")
        self.assertEqual(candidate.local_pref, 400)
        self.assertEqual(
            set(candidate.communities),
            {"pref:silver", "scope:regional"},
        )

        raw = json.loads((self.root / "project_v2.json").read_text(encoding="utf-8"))
        raw["prefixes"][0]["candidates"][0]["local_pref_pct"] = 42.5
        fractional = normalize_project(raw)
        converted = next(
            item
            for item in fractional.prefix_by_id()["pfx-bj-maven"].candidates
            if item.id == "cand-bj-maven-1"
        )
        self.assertEqual(converted.local_pref, 425)

    def test_reference_error_report_has_business_code(self) -> None:
        raw = json.loads((self.root / "project_v1.json").read_text(encoding="utf-8"))
        broken = copy.deepcopy(raw)
        broken["prefixes"][0]["build_node_id"] = "node-does-not-exist"
        broken["prefixes"][1]["candidates"][0]["next_hop_id"] = "nh-missing"
        project = normalize_project(broken)
        report = reference_report(project)
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["code"], "E_REFERENCE")
        self.assertGreaterEqual(report["issue_count"], 2)
        self.assertTrue(
            all(item["code"] == "E_REFERENCE" for item in report["issues"])
        )
        fields = {(item["entity"], item["field"]) for item in report["issues"]}
        self.assertIn(("prefix", "build_node_id"), fields)
        self.assertIn(("candidate", "next_hop_id"), fields)
        with self.assertRaises(ReferenceValidationError) as context:
            normalize_project(broken, strict=True)
        self.assertEqual(context.exception.report["code"], "E_REFERENCE")

    def test_uncatalogued_v2_bit_is_reference_error(self) -> None:
        raw = json.loads((self.root / "project_v2.json").read_text(encoding="utf-8"))
        broken = copy.deepcopy(raw)
        broken["prefixes"][0]["candidates"][0]["community_mask"] |= 1 << 20
        project = normalize_project(broken)
        self.assertTrue(
            any(
                item.code == "E_REFERENCE" and item.field == "community_mask"
                for item in project.reference_issues
            )
        )


if __name__ == "__main__":
    unittest.main()
