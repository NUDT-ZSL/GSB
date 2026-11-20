from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from qxworkbench.bridge import parse_bridge
from qxworkbench.data import read_queries
from qxworkbench.errors import WorkbenchError
from qxworkbench.rules import parse_rules
from qxworkbench.snapshots import SnapshotStore
from qxworkbench.util import stable_json_bytes
from qxworkbench.workbench import BuildConfig, build_and_commit, query_view

from support import prepare_samples, run_cli


class SnapshotAndRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.samples = prepare_samples(self.root)
        self.rules = parse_rules(self.samples / "rules_v3.json")
        self.queries = read_queries(self.samples / "queries.jsonl")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def build(
        self,
        store: SnapshotStore,
        bridge_file: str,
        index_version: int,
        workers: int = 2,
    ):
        return build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=parse_bridge(self.samples / bridge_file),
            index_version=index_version,
            config=BuildConfig(workers=workers),
        )

    def test_atomic_switch_and_old_commit_remain_readable(self) -> None:
        store = SnapshotStore(self.root / "state")
        first = self.build(store, "bridge_v1.json", 1)
        view_before = store.load_current()
        before_bytes = stable_json_bytes(query_view(view_before, self.queries[0]))
        second = self.build(store, "bridge_v2.json", 2)
        self.assertNotEqual(first.commit["commit_id"], second.commit["commit_id"])
        active = store.load_current()
        self.assertEqual(active.commit["bridge_version"], 2)
        self.assertEqual(active.commit["index_version"], 2)
        old = store.load_commit(first.commit["commit_id"])
        self.assertEqual(old.commit["bridge_version"], 1)
        self.assertEqual(
            stable_json_bytes(query_view(old, self.queries[0])),
            before_bytes,
        )
        with self.assertRaises(WorkbenchError) as caught:
            store.load_commit(
                active.commit_id,
                expected_bridge_version=1,
            )
        self.assertEqual(caught.exception.code, "E_VERSION_SKEW")

    def test_same_version_with_changed_content_is_rejected(self) -> None:
        store = SnapshotStore(self.root / "state")
        self.build(store, "bridge_v1.json", 1)
        changed_rule = replace(self.rules.rules[0], enabled=False)
        changed = replace(self.rules, rules=(changed_rule, *self.rules.rules[1:]))
        with self.assertRaises(WorkbenchError) as caught:
            store.save_rules_snapshot(changed, 1)
        self.assertEqual(caught.exception.code, "E_VERSION_SKEW")

    def test_forced_termination_bridge_change_and_resume_match_full_run(self) -> None:
        crash_store = self.root / "crash-state"
        direct_store = self.root / "direct-state"
        common = [
            "--documents",
            str(self.samples / "documents.jsonl"),
            "--rules",
            str(self.samples / "rules_v3.json"),
            "--rule-version",
            "1",
            "--workers",
            "2",
        ]
        first = run_cli(
            [
                "build",
                "--store",
                str(crash_store),
                *common,
                "--bridge",
                str(self.samples / "bridge_v1.json"),
                "--index-version",
                "1",
            ]
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        interrupted = run_cli(
            [
                "build",
                "--store",
                str(crash_store),
                *common,
                "--bridge",
                str(self.samples / "bridge_v2.json"),
                "--index-version",
                "2",
                "--crash-after",
                "1",
            ],
            timeout=20,
        )
        self.assertEqual(interrupted.returncode, 97)
        interrupted_view = SnapshotStore(crash_store).load_current()
        self.assertEqual(interrupted_view.commit["index_version"], 1)

        resumed = run_cli(
            [
                "build",
                "--store",
                str(crash_store),
                *common,
                "--bridge",
                str(self.samples / "bridge_v3.json"),
                "--index-version",
                "2",
            ]
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)

        for bridge_file, index_version in (("bridge_v1.json", "1"), ("bridge_v3.json", "2")):
            direct = run_cli(
                [
                    "build",
                    "--store",
                    str(direct_store),
                    *common,
                    "--bridge",
                    str(self.samples / bridge_file),
                    "--index-version",
                    index_version,
                ]
            )
            self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)

        crash_store_obj = SnapshotStore(crash_store)
        direct_store_obj = SnapshotStore(direct_store)
        crash_view = crash_store_obj.load_current()
        direct_view = direct_store_obj.load_current()
        self.assertEqual(
            crash_view.commit["index_checksum"],
            direct_view.commit["index_checksum"],
        )
        for query in self.queries:
            self.assertEqual(
                stable_json_bytes(query_view(crash_view, query)),
                stable_json_bytes(query_view(direct_view, query)),
                query.query_id,
            )
        reused = set(crash_view.commit["reused_partitions"])
        recomputed = set(crash_view.commit["recomputed_partitions"])
        affected = set(crash_view.commit["affected_partitions"])
        self.assertFalse(reused & recomputed)
        self.assertTrue(affected <= (reused | recomputed))
        self.assertIn("en", reused)
        self.assertIn("fr", recomputed)
        self.assertEqual(affected, {"en", "fr"})

    def test_corrupt_partition_is_reported_as_inconsistent(self) -> None:
        from qxworkbench.selfcheck import run_selfcheck

        store = SnapshotStore(self.root / "state")
        self.build(store, "bridge_v1.json", 1)
        view = store.load_current()
        partition_path = (
            store.index_root
            / f"v{view.index_manifest['version']}"
            / "partitions"
            / "en.json"
        )
        partition_path.write_bytes(partition_path.read_bytes() + b" ")
        with self.assertRaises(WorkbenchError) as caught:
            run_selfcheck(store, queries=self.queries)
        self.assertEqual(caught.exception.code, "E_INCONSISTENT")
        self.assertIn("INDEX_SNAPSHOT", caught.exception.details["categories"])


if __name__ == "__main__":
    unittest.main()
