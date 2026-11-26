from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from deprule.common import canonical_bytes, canonical_hash
from deprule.errors import Codes, DomainError
from deprule.rules import (
    assert_ir_equivalent,
    canonical_ir,
    ir_to_v3_doc,
    read_snapshot,
)
from deprule.runner import run_pipeline
from deprule.seed import seed_workspace
from deprule.store import CommitLock, VersionedStore
from deprule.tasks import load_tasks
from deprule.verify import adjudicate_equivalence, replay_export


class DepruleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="deprule-test-")
        cls.root = Path(cls.temp.name)
        cls.paths = seed_workspace(cls.root / "data")
        cls.tasks = load_tasks(cls.paths["tasks"])
        cls.v1 = read_snapshot(cls.paths["rules_v1"])
        cls.v2 = read_snapshot(cls.paths["rules_v2"])
        cls.v3 = read_snapshot(cls.paths["rules_v3"])
        cls.rev2 = read_snapshot(cls.paths["rules_v2_rev2"])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_reader_error_codes_and_positions(self) -> None:
        cases = [
            ("unknown-version.json", Codes.E_VERSION_UNSUPPORTED, "version"),
            ("bad-pipe-count.json", Codes.E_FORMAT_CORRUPT, "pipe_record[4]"),
            ("zero-window.json", Codes.E_FORMAT_CORRUPT, "pipe_record[9]"),
            ("bad-tag-array.json", Codes.E_FORMAT_CORRUPT, "rule.syntax.tag_set[1]"),
        ]
        for filename, code, field in cases:
            with self.subTest(filename=filename):
                with self.assertRaises(DomainError) as ctx:
                    read_snapshot(Path(self.paths["malformed_dir"]) / filename)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(ctx.exception.details["field"], field)

    def test_three_formats_are_semantically_equivalent(self) -> None:
        assert_ir_equivalent(self.v1["ir"], self.v2["ir"], "v1/v2")
        assert_ir_equivalent(self.v1["ir"], self.v3["ir"], "v1/v3")
        report = adjudicate_equivalence(
            self.tasks,
            {"v1": self.v1, "v2": self.v2, "v3": self.v3},
            self.root / "equiv-state",
        )
        self.assertTrue(report["equivalent"])
        self.assertEqual(len(set(report["parse_semantic_hashes"].values())), 1)

    def test_parse_is_byte_deterministic(self) -> None:
        first, _ = run_pipeline(self.tasks, self.v1, self.root / "det-a", fresh=True)
        second, _ = run_pipeline(self.tasks, self.v1, self.root / "det-b", fresh=True)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        for result in first["results"]:
            self.assertEqual(
                result["stage_checksums"]["stage_1_normalize_validate"],
                result["cache_keys"]["normalize"] and result["stage_checksums"]["stage_1_normalize_validate"],
            )
            self.assertEqual(len(result["roots"]), 1)

    def test_incremental_root_policy_reuses_prefix_and_matches_fresh(self) -> None:
        _, base_ledger = run_pipeline(
            self.tasks, self.v1, self.root / "inc", fresh=True
        )
        incremental, inc_ledger = run_pipeline(
            self.tasks,
            self.rev2,
            self.root / "inc",
            previous_ledger=base_ledger,
            old_snapshot=self.v1,
        )
        self.assertEqual(
            set(inc_ledger["recomputed_kinds"]),
            {"root_crosscheck", "checksums", "export"},
        )
        self.assertEqual(
            set(inc_ledger["reused_kinds"]),
            {"normalize", "preanalysis", "arcs", "labels"},
        )
        self.assertEqual(
            inc_ledger["linguistic_result_changed_sentence_ids"],
            ["en-001"],
        )
        fresh, _ = run_pipeline(self.tasks, self.rev2, self.root / "inc-fresh", fresh=True)
        self.assertEqual(canonical_bytes(incremental), canonical_bytes(fresh))

    def test_replay_ok_and_stale_reports_version_chain(self) -> None:
        result, _ = run_pipeline(self.tasks, self.v1, self.root / "replay-base", fresh=True)
        report = replay_export(result, self.tasks, self.v1, self.root / "replay-state")
        self.assertTrue(report["same_arcs_labels_roots"])
        self.assertGreater(report["verified_arc_count"], 30)

        with self.assertRaises(DomainError) as ctx:
            replay_export(result, self.tasks, self.rev2, self.root / "replay-stale")
        self.assertEqual(ctx.exception.code, Codes.E_STALE_RESULT)
        self.assertEqual(len(ctx.exception.details["stale_sentence_ids"]), len(self.tasks))
        changed = ctx.exception.details["version_chains"][0]["changed_versions"]
        self.assertTrue(any(item["name"] == "root_policy_version" for item in changed))

    def test_checkpoint_crash_recovery_matches_clean_parse(self) -> None:
        state = self.root / "crash-state"
        crash = subprocess.run(
            [
                sys.executable, "-m", "deprule.cli", "parse",
                "--tasks", self.paths["tasks"],
                "--rules", self.paths["rules_v1"],
                "--state-dir", str(state),
                "--out-dir", str(self.root / "crash-out"),
                "--fresh",
                "--crash-mid-stage", "3",
                "--no-commit-ledger",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(crash.returncode, 43, crash.stderr)
        resume = subprocess.run(
            [
                sys.executable, "-m", "deprule.cli", "parse",
                "--tasks", self.paths["tasks"],
                "--rules", self.paths["rules_v1"],
                "--state-dir", str(state),
                "--out-dir", str(self.root / "resume-out"),
                "--no-commit-ledger",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(resume.returncode, 0, resume.stderr)
        ledger = json.loads((self.root / "resume-out" / "invalidation-ledger.json").read_text())
        self.assertEqual(ledger["committed_checkpoint_stage"], 2)
        self.assertEqual(set(ledger["recovered_kinds"]), {"normalize", "preanalysis"})
        self.assertIn("arcs", ledger["recomputed_kinds"])

        clean, _ = run_pipeline(self.tasks, self.v1, self.root / "clean", fresh=True)
        resumed = json.loads((self.root / "resume-out" / "parse-result.json").read_text())
        self.assertEqual(canonical_bytes(resumed), canonical_bytes(clean))

    def test_version_store_rollback_and_nonblocking_busy(self) -> None:
        store = VersionedStore(self.root / "versions", "rules")
        first = store.commit({"revision": 1, "value": "v1"}, incoming_revision=1)
        second = store.commit({"revision": 2, "value": "v2"}, incoming_revision=2)
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        with self.assertRaises(DomainError) as ctx:
            store.commit({"revision": 1, "value": "old"}, incoming_revision=1)
        self.assertEqual(ctx.exception.code, Codes.E_SNAPSHOT_ROLLBACK)

        lock = CommitLock(self.root / "busy.lock")
        lock.acquire_nowait()
        try:
            with self.assertRaises(DomainError) as busy:
                CommitLock(self.root / "busy.lock").acquire_nowait()
            self.assertEqual(busy.exception.code, Codes.E_STAGE_BUSY)
        finally:
            lock.release()

    def test_semantic_drift_identifies_field(self) -> None:
        changed = canonical_ir(revision=1)
        changed["root_policy"]["search_window"] = 1
        changed["root_policy_version"] = "stale"
        with self.assertRaises(DomainError) as ctx:
            assert_ir_equivalent(self.v1["ir"], changed, "unit test")
        self.assertEqual(ctx.exception.code, Codes.E_SEMANTIC_DRIFT)
        self.assertIn("root_policy.search_window", ctx.exception.details["difference_field"])

    def test_parse_drift_category_is_located(self) -> None:
        import deprule.verify as verify

        class FakeRunner:
            def __init__(self, tasks, snapshot, state, **kwargs):
                self.snapshot = snapshot

            def run(self):
                arc = {
                    "arc_id": "arc:h2:d1", "head": 2, "dependent": 1,
                    "label": "nsubj", "direction": "dependent_before_head",
                }
                result = {"results": [{
                    "sentence_id": "s1",
                    "syntax_tree_set": [],
                    "dependency_arcs": [arc],
                    "parse_labels": [{"arc_id": arc["arc_id"], "token_index": 1, "label": "nsubj"}],
                    "roots": [{"index": 2, "label": "root"}],
                    "constraints": {},
                    "stage_checksums": {},
                }]}
                if self.snapshot["format"] == "v3":
                    result["results"][0]["dependency_arcs"][0]["head"] = 3
                return result, {}

        original = verify.PipelineRunner
        verify.PipelineRunner = FakeRunner
        try:
            with self.assertRaises(DomainError) as ctx:
                adjudicate_equivalence(
                    [{"sentence_id": "s1", "lang": "en"}],
                    {"v1": self.v1, "v2": self.v2, "v3": self.v3},
                    self.root / "fake-equiv",
                )
            self.assertEqual(ctx.exception.code, Codes.E_SEMANTIC_DRIFT)
            self.assertEqual(ctx.exception.details["difference_category"], "arc_set")
        finally:
            verify.PipelineRunner = original


if __name__ == "__main__":
    unittest.main()
