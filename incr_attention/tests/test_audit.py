from _bootstrap import ROOT  # noqa: F401
import json
import os
import tempfile
import unittest

import numpy as np

from incrattn.audit import audit_engine, verify_equivalence
from incrattn.config import EngineConfig
from incrattn.engine import IncrementalAttentionEngine
from incrattn.report import write_report_atomic


class AuditTest(unittest.TestCase):
    def test_audit_reports_block_level_errors_and_attribution(self):
        rng = np.random.default_rng(77)
        queries = rng.normal(size=(9, 4)).astype(np.float32)
        config = EngineConfig(
            block_size=4,
            recompute_budget=3,
            max_workers=1,
            snapshot_path=os.path.join(tempfile.mkdtemp(), "a.npz"),
            scale=0.5,
        )
        engine = IncrementalAttentionEngine(config, queries)
        for rows in (4, 4, 1):
            block = rng.normal(size=(rows, 4)).astype(np.float32)
            engine.append_kv(block, block)
        engine.run_to_convergence()
        audit = audit_engine(engine)
        self.assertTrue(audit["all_within_tolerance"])
        self.assertEqual([item["block_index"] for item in audit["blocks"]], [0, 1, 2])
        for block in audit["blocks"]:
            self.assertIn("dominant_error_stage", block)
            self.assertEqual(
                set(block["stage_error_contributions"]),
                {"row_max_selection", "exp_sum_accumulation", "weighted_sum"},
            )
        self.assertTrue(verify_equivalence(engine)["equivalent"])

    def test_report_is_atomic_and_block_sorted(self):
        payload = {
            "blocks": [
                {"block_index": 2, "value": "c"},
                {"block_index": 0, "value": "a"},
                {"block_index": 1, "value": "b"},
            ]
        }
        path = os.path.join(tempfile.mkdtemp(), "report.json")
        write_report_atomic(path, payload)
        on_disk = json.load(open(path, encoding="utf-8"))
        self.assertEqual(
            [item["block_index"] for item in on_disk["blocks"]], [0, 1, 2]
        )
        self.assertFalse(
            any(name.startswith(".report-") for name in os.listdir(os.path.dirname(path)))
        )


if __name__ == "__main__":
    unittest.main()
