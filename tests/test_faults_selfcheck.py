from __future__ import annotations

import copy
import unittest

from query_understanding.errors import E_INCONSISTENT, QUError
from query_understanding.selfcheck import self_check

from tests.common import load_engine, load_small


class FaultSelfCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()
        cls.query = load_small()[0]

    def test_all_four_stages_emit_fault_and_degradation(self):
        for stage in ("normalize", "tokenize", "slot", "intent"):
            result = self.engine.process(self.query, inject={"stage": stage})
            self.assertEqual(len(result["faults"]), 1)
            fault = result["faults"][0]
            self.assertEqual(fault["stage"], stage)
            self.assertTrue(fault["business_code"].startswith("E_INJECT_"))
            self.assertTrue(result["degradation_categories"])
            self.assertTrue(
                result["degradation_categories"][0].startswith(stage + ":")
            )
            self.assertTrue(result["self_check"]["ok"])

    def test_input_can_trigger_fault(self):
        query = dict(self.query)
        query["_fault_stage"] = "slot"
        query["_fault_code"] = "E_INPUT_INJECT_SLOT"
        result = self.engine.process(query)
        self.assertEqual(result["faults"][0]["trigger"], "input")
        self.assertEqual(
            result["faults"][0]["business_code"], "E_INPUT_INJECT_SLOT"
        )
        self.assertEqual(result["degradation_categories"], ["slot:slots_dropped"])

    def test_self_check_detects_token_mismatch_category(self):
        result = copy.deepcopy(self.engine.process(self.query))
        result["tokens"][0]["text"] = "损坏"
        with self.assertRaises(QUError) as context:
            self_check(result, self.engine.taxonomy)
        self.assertEqual(context.exception.code, E_INCONSISTENT)
        self.assertEqual(context.exception.category, "token_text_mismatch")

    def test_self_check_detects_slot_overlap_category(self):
        result = copy.deepcopy(self.engine.process(self.query))
        result["slots"].append(
            {
                **result["slots"][0],
                "slot_id": "overlap",
                "start": result["slots"][0]["start"],
                "end": result["slots"][0]["end"],
            }
        )
        with self.assertRaises(QUError) as context:
            self_check(result, self.engine.taxonomy)
        self.assertEqual(context.exception.code, E_INCONSISTENT)
        self.assertIn(
            context.exception.category,
            {"slot_overlap", "slot_schema_mismatch"},
        )


if __name__ == "__main__":
    unittest.main()

