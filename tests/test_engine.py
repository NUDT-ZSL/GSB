"""Unit tests for the document query / fragment index engine.

Run with: .venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import tempfile
import unittest

from docengine.config import Config
from docengine.engine import cmd_merge, cmd_query, cmd_resume, open_world
from docengine.errors import BusinessError, ErrorCode
from docengine.fragments import FragmentStore
from docengine.merge import build_unified_index, merge_report
from docengine.snapshot_v1 import load_v1
from docengine.snapshot_v2 import load_v2
from docengine.stablejson import digest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "samples", "config.json")


def fresh_config(test):
    """Config pointing at the fixed samples with a private state dir."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    state_dir = tempfile.mkdtemp(prefix="docengine-test-")
    test.addCleanup(shutil.rmtree, state_dir, True)
    raw["base_dir"] = ROOT
    raw["state_db"] = os.path.join(state_dir, "state.sqlite")
    raw["lock_file"] = os.path.join(state_dir, "engine.lock")
    derived = os.path.join(state_dir, "config.json")
    with open(derived, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    return Config(derived)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = fresh_config(self)
        self.world = open_world(self.config)

    def test_fixed_samples_validate(self):
        self.assertEqual(len(self.world["v1"]["documents"]), 7)
        self.assertEqual(len(self.world["v2"]["documents"]), 6)
        self.assertEqual(sorted(self.world["v2"]["tombstones"]), ["doc-005"])

    def _v1_error(self, mutate):
        path = os.path.join(ROOT, "samples", "v1", "snapshot_v1.json")
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        mutate(raw)
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        self.addCleanup(os.unlink, tmp.name)
        json.dump(raw, tmp)
        tmp.close()
        store = FragmentStore(self.config.fragment_dir, [])
        with self.assertRaises(BusinessError) as ctx:
            load_v1(tmp.name, store)
        return ctx.exception

    def test_v1_duplicate_doc(self):
        def mutate(raw):
            raw["documents"].append(dict(raw["documents"][0]))

        self.assertEqual(
            self._v1_error(mutate).code, ErrorCode.E_V1_DUPLICATE_DOC
        )

    def test_v1_field_type(self):
        def mutate(raw):
            raw["documents"][0]["fields"]["bad"] = True

        self.assertEqual(self._v1_error(mutate).code,
                         ErrorCode.E_V1_FIELD_TYPE)

    def test_v1_span_range(self):
        def mutate(raw):
            raw["documents"][0]["spans"][0]["length"] = 10 ** 6

        self.assertEqual(self._v1_error(mutate).code,
                         ErrorCode.E_V1_SPAN_RANGE)

    def test_v1_inverted_orphan(self):
        def mutate(raw):
            raw["inverted"].append(
                {"doc_id": "ghost", "field": "status", "value": "x"}
            )

        self.assertEqual(
            self._v1_error(mutate).code, ErrorCode.E_V1_INVERTED_ORPHAN
        )

    def test_v2_shard_hash(self):
        manifest = dict(self.world["manifest"])
        manifest["shards"] = [dict(s) for s in manifest["shards"]]
        manifest["shards"][0]["sha256"] = "0" * 64
        with self.assertRaises(BusinessError) as ctx:
            load_v2(
                self.config.v2_shard_dir,
                self.config.v2_meta_db,
                manifest["shards"],
                self.world["fragment_store"],
            )
        self.assertEqual(ctx.exception.code, ErrorCode.E_V2_SHARD_HASH)
        self.assertIn("shard", ctx.exception.details)

    def test_fragment_checksum_error_has_diagnostics(self):
        store = self.world["fragment_store"]
        corrupt = None
        for entry in store.manifest.values():
            try:
                store.read_verified(entry["checksum"])
            except BusinessError as exc:
                corrupt = exc
        self.assertIsNotNone(corrupt)
        self.assertEqual(corrupt.code, ErrorCode.E_FRAGMENT_CHECKSUM)
        self.assertIn("checksum", corrupt.details)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.config = fresh_config(self)
        self.world = open_world(self.config)

    def test_adjudication_rules(self):
        report = merge_report(self.world["index"])
        decisions = {
            (d["doc_id"], d["field"]): d for d in report["decisions"]
        }
        self.assertEqual(decisions[("doc-001", "score")]["rule"], "version")
        self.assertEqual(decisions[("doc-001", "score")]["source"], "v2")
        self.assertEqual(decisions[("doc-006", "tag")]["rule"], "clock")
        self.assertEqual(decisions[("doc-006", "tag")]["value"], "clock-v2")
        self.assertEqual(decisions[("doc-007", "tag")]["rule"], "clock")
        self.assertEqual(decisions[("doc-007", "tag")]["value"], "clock-v1")
        self.assertEqual(decisions[("doc-008", "tag")]["rule"], "priority")
        self.assertEqual(decisions[("doc-008", "tag")]["source"], "v2")
        self.assertEqual(report["tombstoned"], ["doc-005"])
        self.assertNotIn("doc-005", self.world["index"].docs)

    def test_load_order_convergence(self):
        v1, v2 = self.world["v1"], self.world["v2"]
        forward = digest(merge_report(build_unified_index(v1, v2)))
        v1_rev = dict(v1)
        v1_rev["documents"] = dict(reversed(list(v1["documents"].items())))
        v2_rev = dict(v2)
        v2_rev["documents"] = dict(reversed(list(v2["documents"].items())))
        backward = digest(merge_report(build_unified_index(v1_rev, v2_rev)))
        self.assertEqual(forward, backward)

    def test_bidirectional_index(self):
        index = self.world["index"]
        score = index.field_index("score")
        self.assertEqual(score.eq(20), ["doc-001"])
        self.assertEqual(score.range(10, 50), ["doc-001", "doc-002"])
        status = index.field_index("status")
        self.assertEqual(status.eq("archived"), ["doc-002"])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.config = fresh_config(self)

    def test_merge_idempotent_and_query_replay(self):
        first = cmd_merge(self.config, 3100)
        second = cmd_merge(self.config, 3100)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["generation"], second["generation"])
        q1 = cmd_query(self.config, 3200, "legal.json")
        q2 = cmd_query(self.config, 3200, "legal.json",
                       batch_no=q1["batch_no"])
        self.assertTrue(q2["idempotent"])
        self.assertEqual(q1["result_digest"], q2["result_digest"])

    def test_query_requires_committed_index(self):
        with self.assertRaises(BusinessError) as ctx:
            cmd_query(self.config, 3200, "legal.json")
        self.assertEqual(ctx.exception.code, ErrorCode.E_NO_INDEX)

    def test_clock_regression_rejected(self):
        cmd_merge(self.config, 3100)
        cmd_query(self.config, 3200, "legal.json")
        with self.assertRaises(BusinessError) as ctx:
            cmd_query(self.config, 1000, "legal.json")
        self.assertEqual(ctx.exception.code, ErrorCode.E_CLOCK_REGRESSION)

    def test_crash_resume_replays_batch(self):
        from docengine.state import StateStore

        cmd_merge(self.config, 3100)
        state = StateStore(self.config.state_db)
        with open(os.path.join(self.config.templates_dir, "legal.json"),
                  encoding="utf-8") as fh:
            templates = json.load(fh)
        state.begin_batch(1, 1, digest(templates),
                          json.dumps(templates, sort_keys=True), 3200, 0)
        state.close()  # simulates a kill with a persisted 'running' batch
        out = cmd_resume(self.config, 3300)
        actions = out["recovery_actions"]
        self.assertEqual(actions[0]["action"], "replayed_batch")
        self.assertEqual(actions[0]["batch_no"], 1)
        batches = out["state"]["batches"]
        self.assertEqual(batches[0]["status"], "committed")

    def test_crash_resume_discards_half_merge(self):
        from docengine.state import StateStore

        cmd_merge(self.config, 3100)
        state = StateStore(self.config.state_db)
        state.stage_generation_crash("f" * 64, "e" * 64, 3150)
        state.close()
        out = cmd_resume(self.config, 3300)
        self.assertEqual(
            out["recovery_actions"],
            [{"action": "discard_half_merge", "generation": 2}],
        )
        self.assertEqual(out["state"]["pointer"]["generation"], 1)


if __name__ == "__main__":
    unittest.main()
