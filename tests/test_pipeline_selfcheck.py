import json
import os
import shutil
import tempfile
import unittest

from llm_workbench import generate_samples
from llm_workbench.errors import ErrorCode, WorkbenchError
from llm_workbench.pipeline import run_pipeline
from llm_workbench.snapshots import SnapshotStore


class TestPipelineEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.work = os.path.join(self.tmp, "work")
        generate_samples.generate(self.data, n_requests=16, vocab_size=128)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_pipeline_ok(self):
        rep = run_pipeline(self.data, self.work)
        self.assertTrue(rep["selfcheck"]["ok"], rep["selfcheck"]["errors"])
        self.assertTrue(rep["ok"])
        self.assertGreater(len(rep["results"]), 0)
        store = SnapshotStore(os.path.join(self.work, "snapshots"))
        for stype in ("artifacts", "ledger", "decisions"):
            self.assertEqual(store.get(stype)["version"], 1)

    def test_rejected_requests_absent_from_results(self):
        rep = run_pipeline(self.data, self.work)
        rejected = {r["request_id"] for r in rep["admission"]["rejected"]}
        self.assertTrue(rejected.isdisjoint(rep["results"].keys()))

    def test_crash_recovery_pipeline(self):
        rep = run_pipeline(self.data, self.work, crash_after=2)
        self.assertTrue(rep["run"]["recovery"]["forced_termination_simulated"])
        self.assertEqual(rep["run"]["recovery"]["resumed_from_index"], 2)
        self.assertTrue(rep["selfcheck"]["ok"], rep["selfcheck"]["errors"])

    def test_recovered_results_match_clean_run(self):
        rep_clean = run_pipeline(self.data, os.path.join(self.tmp, "w1"))
        rep_rec = run_pipeline(self.data, os.path.join(self.tmp, "w2"), crash_after=3)
        for rid, res in rep_clean["results"].items():
            self.assertEqual(json.dumps(res, sort_keys=True),
                             json.dumps(rep_rec["results"][rid], sort_keys=True))

    def test_selfcheck_detects_tampering(self):
        rep = run_pipeline(self.data, self.work)
        self.assertTrue(rep["selfcheck"]["ok"])
        ckpt = os.path.join(self.work, "checkpoint.json")
        with open(ckpt, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        rid = state["committed"][0]
        state["results"][rid]["tokens"][0] = (state["results"][rid]["tokens"][0] + 1) % 128
        state["results"][rid]["steps"][0]["chosen_token"] = state["results"][rid]["tokens"][0]
        with open(ckpt, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        with self.assertRaises(WorkbenchError) as ctx:
            run_pipeline(self.data, self.work)
        self.assertEqual(ctx.exception.code, ErrorCode.E_BATCH_INCONSISTENT)
        self.assertEqual(ctx.exception.context["request_id"], rid)
        self.assertEqual(ctx.exception.context["first_diverging_step"], 0)

    def test_vocab_missing_stops_pipeline(self):
        os.remove(os.path.join(self.data, "vocab.json"))
        rep = run_pipeline(self.data, self.work)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["errors"][0]["code"], ErrorCode.E_VOCAB_MISSING)


if __name__ == "__main__":
    unittest.main()
