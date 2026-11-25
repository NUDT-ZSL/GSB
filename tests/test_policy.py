from __future__ import annotations

import json
import sqlite3
import unittest

from topodiag.engine import build_workspace
from topodiag.errors import BusinessError
from topodiag.policy import load_v1_policy, load_v2_package, merge_v1_v2
from topodiag.sample import generate_samples
from topodiag.storage import runtime_db_path

from .helpers import make_workdir


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = make_workdir("topodiag-policy-")
        generate_samples(self.workdir)
        self.samples = self.workdir / "samples"

    def test_cross_generation_merge_records_decision_and_v1_conversion(self) -> None:
        v1 = load_v1_policy(self.samples / "policies" / "v1.json")
        v2 = load_v2_package(self.samples / "policies" / "v2")
        merged = merge_v1_v2(v1, v2, injected_clock_ms=12_000)
        self.assertIn("archive", merged)
        self.assertEqual(merged["archive"].source_format, "v1")
        self.assertEqual(merged["archive"].effective_at_ms, 17_000)
        self.assertEqual(
            merged["archive"].conversion["scale_ms_per_second"],
            1000,
        )
        self.assertEqual(
            merged["archive"].conversion["pre_round_value"],
            5,
        )
        self.assertEqual(
            merged["catalog"].decision_basis,
            "source_format_priority",
        )
        self.assertEqual(merged["analytics"].decision_basis, "higher_logical_clock")
        self.assertEqual(merged["catalog"].source_format, "v2")

    def test_build_is_idempotent_and_persists_merge_decisions(self) -> None:
        first = build_workspace(self.workdir)
        self.assertEqual(first["mode"], "full")
        db = runtime_db_path(self.workdir)
        with sqlite3.connect(str(db)) as connection:
            before = connection.execute(
                """
                SELECT template_name, decision_hash, decision_json
                FROM merge_decisions
                ORDER BY template_name
                """
            ).fetchall()
        second = build_workspace(self.workdir)
        self.assertEqual(second["mode"], "noop")
        with sqlite3.connect(str(db)) as connection:
            after = connection.execute(
                """
                SELECT template_name, decision_hash, decision_json
                FROM merge_decisions
                ORDER BY template_name
                """
            ).fetchall()
        self.assertEqual(before, after)

    def test_incremental_update_recomputes_only_affected_branch(self) -> None:
        first = build_workspace(self.workdir)
        before = {row["template_name"]: row for row in first["closures"]}
        update = build_workspace(
            self.workdir,
            policy_package=self.samples / "policies" / "v2-update",
        )
        self.assertEqual(update["mode"], "incremental")
        self.assertEqual(update["policy"]["policy_generation"], 3)
        self.assertEqual(len(update["invalidations"]), 2)
        analytics_event = next(
            event
            for event in update["invalidations"]
            if event["trigger"]["template_name"] == "analytics"
        )
        self.assertEqual(analytics_event["affected_downstream"], ["report"])
        self.assertEqual(analytics_event["chains"], [["analytics", "report"]])
        after = {row["template_name"]: row for row in update["closures"]}
        for unaffected in ("orders", "frontend", "catalog", "base"):
            self.assertEqual(
                before[unaffected]["config_fingerprint"],
                after[unaffected]["config_fingerprint"],
            )
            self.assertEqual(
                before[unaffected]["revision"],
                after[unaffected]["revision"],
            )
        self.assertNotEqual(
            before["report"]["config_fingerprint"],
            after["report"]["config_fingerprint"],
        )
        self.assertEqual(after["report"]["revision"], before["report"]["revision"] + 1)
        retired_event = next(
            event
            for event in update["invalidations"]
            if event["trigger"]["template_name"] == "retired"
        )
        self.assertEqual(retired_event["trigger"]["policy_generation"], 3)
        self.assertEqual(retired_event["trigger"]["logical_clock"], 13_000)

    def test_reapplying_same_package_is_noop(self) -> None:
        build_workspace(self.workdir)
        package = self.samples / "policies" / "v2-update"
        first = build_workspace(self.workdir, policy_package=package)
        second = build_workspace(self.workdir, policy_package=package)
        self.assertEqual(first["mode"], "incremental")
        self.assertEqual(second["mode"], "noop")
        self.assertEqual(
            first["policy"]["policy_version_id"],
            second["policy"]["policy_version_id"],
        )

    def test_older_policy_package_is_rejected(self) -> None:
        build_workspace(self.workdir)
        build_workspace(
            self.workdir,
            policy_package=self.samples / "policies" / "v2-update",
        )
        with self.assertRaises(BusinessError) as caught:
            build_workspace(
                self.workdir,
                policy_package=self.samples / "policies" / "v2",
            )
        self.assertEqual(caught.exception.code, "POLICY_ROLLBACK")

    def test_build_clock_rollback_is_rejected(self) -> None:
        build_workspace(self.workdir)
        with self.assertRaises(BusinessError) as caught:
            build_workspace(self.workdir, clock_ms=10_000)
        self.assertEqual(caught.exception.code, "CLOCK_ROLLBACK")
        self.assertIn("watermark", caught.exception.diagnostics)


if __name__ == "__main__":
    unittest.main()
