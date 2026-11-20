from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qxworkbench.bridge import parse_bridge
from qxworkbench.data import read_queries
from qxworkbench.errors import WorkbenchError
from qxworkbench.rules import parse_rules
from qxworkbench.selfcheck import run_selfcheck
from qxworkbench.snapshots import SnapshotStore
from qxworkbench.workbench import BuildConfig, build_and_commit

from support import prepare_samples


class ResourceAndSelfcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.samples = prepare_samples(self.root)
        self.rules = parse_rules(self.samples / "rules_v3.json")
        self.bridge = parse_bridge(self.samples / "bridge_v1.json")
        self.queries = read_queries(self.samples / "queries.jsonl")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_parallel_budget_degrade_marker(self) -> None:
        store = SnapshotStore(self.root / "state")
        result = build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge,
            index_version=1,
            config=BuildConfig(
                workers=2,
                max_parallel_partitions=1,
                parallel_policy="degrade",
            ),
        )
        self.assertIn("DEGRADED_LIMITED_PARALLELISM", result.index_manifest["degraded"])

    def test_parallel_budget_strict_error(self) -> None:
        store = SnapshotStore(self.root / "state")
        with self.assertRaises(WorkbenchError) as caught:
            build_and_commit(
                store,
                documents_path=self.samples / "documents.jsonl",
                ruleset=self.rules,
                rule_version=1,
                bridge=self.bridge,
                index_version=1,
                config=BuildConfig(
                    workers=2,
                    max_parallel_partitions=1,
                    parallel_policy="error",
                ),
            )
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")

    def test_posting_budget_strict_error(self) -> None:
        store = SnapshotStore(self.root / "state")
        with self.assertRaises(WorkbenchError) as caught:
            build_and_commit(
                store,
                documents_path=self.samples / "documents.jsonl",
                ruleset=self.rules,
                rule_version=1,
                bridge=self.bridge,
                index_version=1,
                config=BuildConfig(workers=1, max_postings=5),
            )
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")

    def test_selfcheck_recomputes_scores_and_versions(self) -> None:
        store = SnapshotStore(self.root / "state")
        build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge,
            index_version=1,
            config=BuildConfig(workers=2),
        )
        report = run_selfcheck(store, queries=self.queries)
        self.assertTrue(report["ok"])
        self.assertEqual(report["categories"], [])
        self.assertEqual(report["checked_queries"], len(self.queries))
        self.assertGreater(report["checked_contributions"], 0)
        self.assertEqual(report["checked_postings"], report["posting_count"])

    def test_tampered_commit_is_inconsistent(self) -> None:
        store = SnapshotStore(self.root / "state")
        result = build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge,
            index_version=1,
            config=BuildConfig(workers=2),
        )
        commit_path = store.commits_root / f"{result.commit['commit_id']}.json"
        commit = json.loads(commit_path.read_text(encoding="utf-8"))
        commit["reused_partitions"] = ["en"]
        commit["recomputed_partitions"] = ["en"]
        commit_path.write_text(
            json.dumps(
                commit,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(WorkbenchError) as caught:
            run_selfcheck(store, queries=self.queries)
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")


if __name__ == "__main__":
    unittest.main()
