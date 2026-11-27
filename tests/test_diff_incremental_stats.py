from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from build_routing.diff import compare_projects, load_known
from build_routing.engine import ReplayEngine
from build_routing.generator import generate_artifacts
from build_routing.model import ALL_FILTERED, SINGLE_CANDIDATE
from build_routing.normalize import load_project
from build_routing.selector import select_prefix
from build_routing.stats import build_availability_stats


class DiffIncrementalStatsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        generate_artifacts(cls.root, seed=20261128)
        cls.v1 = load_project(cls.root / "project_v1.json")
        cls.v2 = load_project(cls.root / "project_v2.json")
        cls.events = json.loads((cls.root / "events.json").read_text(encoding="utf-8"))
        cls.known = load_known(cls.root / "known_differences.json")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_diff_attribution_exactly_matches_known_injections(self) -> None:
        report = compare_projects(self.v1, self.v2, self.known)
        self.assertTrue(report["known_validation"]["match"])
        self.assertEqual(
            set(report["diff_prefixes"]),
            {"pfx-bj-maven", "pfx-sh-pypi"},
        )
        by_prefix = {item["prefix_id"]: item for item in report["differences"]}
        self.assertEqual(
            by_prefix["pfx-bj-maven"]["reasons"],
            ["community_representation"],
        )
        self.assertEqual(
            by_prefix["pfx-sh-pypi"]["reasons"],
            ["local_pref_scale"],
        )
        self.assertTrue(
            all(
                item["known_difference_match"]
                for item in report["differences"]
            )
        )
        self.assertGreater(len(by_prefix["pfx-bj-maven"]["field_diffs"]), 0)
        self.assertGreater(len(by_prefix["pfx-sh-pypi"]["field_diffs"]), 0)

    def test_incremental_policy_recompute_preserves_unaffected_prefixes(self) -> None:
        engine = ReplayEngine(self.v1, self.events)
        engine.run()
        before_states = {
            prefix.id: engine.states[prefix.id].to_dict()
            for prefix in self.v1.prefixes
        }
        before_histories = {
            prefix.id: [item.to_dict() for item in engine.switches[prefix.id]]
            for prefix in self.v1.prefixes
        }
        before_counts = dict(engine.recompute_counts)
        report = engine.recompute_incremental_policy(
            {"community_weights": {"pref:gold": 50}}
        )
        self.assertTrue(report["full_recompute_match"])
        self.assertTrue(report["unaffected_preserved"])
        self.assertGreater(report["recomputed_count"], 0)
        self.assertGreater(report["unchanged_count"], 0)
        for prefix_id in report["unchanged_prefix_ids"]:
            self.assertEqual(before_states[prefix_id], engine.states[prefix_id].to_dict())
            self.assertEqual(
                before_histories[prefix_id],
                [item.to_dict() for item in engine.switches[prefix_id]],
            )
            self.assertEqual(
                before_counts[prefix_id],
                engine.recompute_counts[prefix_id],
            )

        project_with_new_policy = replace(self.v1, policy=engine.policy)
        for prefix in self.v1.prefixes:
            full = select_prefix(prefix, project_with_new_policy)
            incremental = engine.states[prefix.id].result
            self.assertEqual(
                full.selected_fingerprint(),
                incremental.selected_fingerprint(),
                prefix.id,
            )

    def test_stats_distinguish_all_filtered_and_single_candidate(self) -> None:
        engine = ReplayEngine(self.v1, self.events)
        initial = build_availability_stats(
            self.v1,
            {prefix.id: engine.states[prefix.id].result for prefix in self.v1.prefixes},
        )
        self.assertEqual(initial["totals"]["reason_counts"][SINGLE_CANDIDATE], 2)
        self.assertEqual(initial["totals"]["reason_counts"][ALL_FILTERED], 0)
        self.assertEqual(len(initial["nodes"]), 3)
        self.assertEqual(len(initial["regions"]), 3)
        self.assertEqual(
            {item["region_id"] for item in initial["regions"]},
            {"region-north", "region-east", "region-eu"},
        )

        engine.run(max_events=2)
        at_200 = build_availability_stats(
            self.v1,
            {prefix.id: engine.states[prefix.id].result for prefix in self.v1.prefixes},
        )
        self.assertEqual(at_200["totals"]["reason_counts"][ALL_FILTERED], 2)
        issue_prefixes = {
            item["prefix_id"]
            for item in at_200["totals"]["issues"]
            if item["issue"] == "no_available_candidate"
        }
        self.assertEqual(issue_prefixes, {"pfx-bj-pypi", "pfx-bj-npm"})
        self.assertTrue(
            any(
                item["issue"] == "no_backup_path"
                for item in at_200["totals"]["issues"]
            )
        )


if __name__ == "__main__":
    unittest.main()
