from __future__ import annotations

import copy
import unittest
from unittest import mock

from alarm_domain.errors import DomainError
from alarm_domain.session import AlarmSession, first_analysis_difference

from .helpers import CHANGESET, EVENTS, RULES, TOPOLOGY


class IncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = AlarmSession.from_files(RULES, TOPOLOGY, EVENTS, window_ms=500)

    def test_rule_update_invalidates_only_affected_cluster_and_matches_full(self) -> None:
        before = self.session.analyses
        reused_before = {
            item["cluster_id"]: item for item in before if item["cluster_id"] != "cl-e275514f98f04dff"
        }
        trace = self.session.apply_changeset(CHANGESET)
        self.assertEqual(trace["affected_services"], ["payments"])
        self.assertEqual(trace["invalidated_clusters"], ["cl-e275514f98f04dff"])
        self.assertEqual((trace["recomputed_count"], trace["reused_count"], trace["total_cluster_count"]), (1, 2, 3))
        self.assertTrue(trace["counts_consistent"])
        self.assertTrue(trace["reused_hashes_verified"])
        self.assertTrue(trace["full_equivalence"])
        self.assertEqual(trace["seq"], 1)
        roots = {item["cluster_id"]: item["root_cause"]["selected_service"] for item in self.session.analyses}
        self.assertEqual(roots["cl-e275514f98f04dff"], "payments")
        for cluster_id, old in reused_before.items():
            new = next(item for item in self.session.analyses if item["cluster_id"] == cluster_id)
            self.assertEqual(old, new)
        full = self.session.recompute_all()
        self.assertIsNone(first_analysis_difference(full, self.session.analyses))
        self.assertEqual(self.session.invalidation_trace[-1], trace)

    def test_topology_edge_add_is_local_and_verified(self) -> None:
        changeset = {
            "changeset_id": "cs-add-inventory-ledger",
            "rules": [],
            "edges": [
                {
                    "op": "add",
                    "edge": {
                        "upstream": "inventory",
                        "downstream": "ledger",
                        "weight": 0.7,
                    },
                }
            ],
        }
        trace = self.session.apply_changeset(changeset)
        self.assertIn("cl-e275514f98f04dff", trace["invalidated_clusters"])
        self.assertEqual(trace["reused_count"], 2)
        self.assertEqual(trace["recomputed_count"] + trace["reused_count"], 3)
        self.assertTrue(trace["full_equivalence"])
        self.assertIsNone(
            first_analysis_difference(self.session.recompute_all(), self.session.analyses)
        )

    def test_edge_weight_update_is_local_and_full_equivalent(self) -> None:
        changeset = {
            "changeset_id": "cs-weight-only",
            "rules": [],
            "edges": [
                {
                    "op": "update",
                    "edge": {
                        "upstream": "orders",
                        "downstream": "payments",
                        "weight": 0.5,
                    },
                }
            ],
        }
        before_topology_version = self.session.topology_version
        trace = self.session.apply_changeset(changeset)
        self.assertEqual(trace["invalidated_clusters"], ["cl-e275514f98f04dff"])
        self.assertEqual((trace["recomputed_count"], trace["reused_count"]), (1, 2))
        self.assertNotEqual(self.session.topology_version, before_topology_version)
        self.assertIsNone(
            first_analysis_difference(self.session.recompute_all(), self.session.analyses)
        )

    def test_rule_delete_with_retained_events_inactivates_them(self) -> None:
        changeset = {
            "changeset_id": "cs-delete-mailer-and-edge",
            "rules": [{"op": "delete", "rule_id": "R-MAIL-Q"}],
            "edges": [
                {
                    "op": "delete",
                    "edge": {"upstream": "mailer", "downstream": "smtp"},
                }
            ],
        }
        trace = self.session.apply_changeset(changeset)
        self.assertEqual(trace["affected_services"], ["mailer", "smtp"])
        self.assertEqual(len(self.session.events), 10)
        self.assertNotIn("mailer", [rule.service for rule in self.session.rules])
        self.assertEqual(len(self.session.analyses), 2)
        self.assertEqual(trace["recomputed_count"] + trace["reused_count"], 2)
        self.assertTrue(trace["full_equivalence"])
        self.assertIsNone(
            first_analysis_difference(self.session.recompute_all(), self.session.analyses)
        )

    def test_deleting_depended_service_reports_e_depend(self) -> None:
        changeset = {
            "changeset_id": "cs-delete-payments-service",
            "rules": [
                {"op": "delete", "rule_id": "R-PAY-5XX"},
                {"op": "delete", "rule_id": "R-PAY-LAT"},
            ],
            "edges": [],
        }
        old_hash = self.session.analysis_hash
        with self.assertRaises(DomainError) as caught:
            self.session.apply_changeset(changeset)
        self.assertEqual(caught.exception.code, "E_DEPEND")
        self.assertEqual(caught.exception.detail["service"], "payments")
        self.assertEqual(caught.exception.detail["dependents"], ["orders"])
        self.assertEqual(self.session.analysis_hash, old_hash)
        self.assertEqual(self.session.invalidation_trace, [])

    def test_e_incr_reports_first_differing_cluster(self) -> None:
        original_full = self.session._full_analysis

        def tampered_full(**kwargs):
            analyses = copy.deepcopy(original_full(**kwargs))
            analyses[0]["deduplicated"]["retained_alerts"] = []
            return analyses

        with mock.patch.object(self.session, "_full_analysis", side_effect=tampered_full):
            with self.assertRaises(DomainError) as caught:
                self.session.apply_changeset(CHANGESET)
        self.assertEqual(caught.exception.code, "E_INCR")
        self.assertIn("first_difference_cluster_id", caught.exception.detail)
        self.assertEqual(self.session.invalidation_trace, [])

    def test_two_updates_have_stable_trace_order(self) -> None:
        first = self.session.apply_changeset(CHANGESET)
        second = self.session.apply_changeset(
            {
                "changeset_id": "cs-pay-severity-back",
                "rules": [
                    {
                        "op": "update",
                        "rule_id": "R-PAY-5XX",
                        "changes": {"severity": "WARNING"},
                    }
                ],
                "edges": [],
            }
        )
        self.assertEqual([item["seq"] for item in self.session.invalidation_trace], [1, 2])
        self.assertEqual(first["invalidated_clusters"], second["invalidated_clusters"])
        self.assertEqual(first["affected_services"], ["payments"])


if __name__ == "__main__":
    unittest.main()
