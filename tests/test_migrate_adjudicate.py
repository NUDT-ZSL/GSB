"""迁移器与跨格式裁决测试。"""

import shutil
import tempfile
import unittest

from detwb.adjudicate import adjudicate, replay_report, save_inputs, save_report
from detwb.errors import Err
from detwb.formats import write_v1, write_v2, write_v3
from detwb.migrate import (DEFAULT_DROP_RULES, expressible_fields_unchanged,
                           migrate_rich_to_v1, migrate_v1_to_rich)
from tests.helpers import make_records, make_samples_gt, make_vocab


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.vocab = make_vocab()
        self.records = make_records()

    def test_v1_upgrade_provenance(self):
        v1_records, _ = migrate_rich_to_v1(self.records)
        rich, provenance = migrate_v1_to_rich(v1_records, self.vocab)
        sources = {(p["field"], p["source"]) for p in provenance}
        self.assertIn(("class_name", "vocab"), sources)
        self.assertIn(("difficulty", "default"), sources)
        self.assertIn(("conf_lo", "default"), sources)
        self.assertIn(("vocab_version", "header"), sources)
        self.assertEqual(rich[0]["class_name"], "car")
        self.assertEqual(rich[0]["difficulty"], "unknown")
        self.assertEqual(rich[0]["conf_lo"], 0.0)
        self.assertEqual(rich[0]["conf_hi"], 1.0)

    def test_downgrade_drop_manifest(self):
        _, drops = migrate_rich_to_v1(self.records)
        by_field = {}
        for d in drops:
            by_field.setdefault(d["field"], set()).add(d["rule"])
        self.assertIn(DEFAULT_DROP_RULES["occluded"], by_field["occluded"])
        self.assertIn(DEFAULT_DROP_RULES["conf_lo"], by_field["conf_lo"])
        self.assertIn(DEFAULT_DROP_RULES["conf_hi"], by_field["conf_hi"])
        self.assertIn(DEFAULT_DROP_RULES["difficulty"], by_field["difficulty"])
        restorable = {(d["record"], d["field"]): d["restorable"]
                      for d in drops}
        self.assertTrue(restorable[("s0#1", "occluded")])   # 默认值可还原
        self.assertFalse(restorable[("s0#0", "conf_lo")])   # 非默认值不可还原

    def test_roundtrip_preserves_expressible(self):
        v1_records, _ = migrate_rich_to_v1(self.records)
        rich, _ = migrate_v1_to_rich(v1_records, self.vocab)
        v1_zero, _ = migrate_rich_to_v1(rich)
        rich_again, _ = migrate_v1_to_rich(v1_zero, self.vocab)
        self.assertIsNone(expressible_fields_unchanged(rich, rich_again))


class TestAdjudicate(unittest.TestCase):
    def setUp(self):
        self.vocab = make_vocab()
        self.records = make_records()
        self.samples_gt = make_samples_gt()
        self.dir = tempfile.mkdtemp(prefix="detwb_adj_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _texts(self, records):
        return (write_v1(records, 1), write_v2(records, 1),
                write_v3(records, 1))

    def test_drift_reported_for_nondefault_fields(self):
        t1, t2, t3 = self._texts(self.records)
        report = adjudicate(t1, t2, t3, self.vocab, self.samples_gt)
        self.assertEqual(report["verdict"], "DRIFT")
        self.assertEqual(report["error"]["code"], Err.SEMANTIC_DRIFT)
        fields = set(report["error"]["unrestorable_fields"])
        self.assertTrue({"conf_lo", "conf_hi", "difficulty"} <= fields)

    def test_equivalent_when_defaults(self):
        records = []
        for r in self.records:
            r = dict(r)
            r["difficulty"] = "unknown"
            r["occluded"] = False
            r["conf_lo"] = 0.0
            r["conf_hi"] = 1.0
            records.append(r)
        t1, t2, t3 = self._texts(records)
        report = adjudicate(t1, t2, t3, self.vocab, self.samples_gt)
        self.assertEqual(report["verdict"], "EQUIVALENT")
        self.assertIsNone(report["error"])

    def test_shared_semantics_identical(self):
        t1, t2, t3 = self._texts(self.records)
        report = adjudicate(t1, t2, t3, self.vocab, self.samples_gt)
        self.assertEqual(report["shared_semantics"]["assignment"], "identical")
        self.assertEqual(report["shared_semantics"]["tp_fp_fn"], "identical")
        self.assertEqual(report["shared_semantics"]["aggregates"], "identical")

    def test_replay_from_sidecar(self):
        t1, t2, t3 = self._texts(self.records)
        report = adjudicate(t1, t2, t3, self.vocab, self.samples_gt)
        adjudication_id, _ = save_report(report, self.dir)
        save_inputs(adjudication_id, self.dir, t1, t2, t3, self.vocab,
                    self.samples_gt)
        replayed = replay_report(adjudication_id, self.dir)
        self.assertEqual(replayed["verdict"], report["verdict"])


if __name__ == "__main__":
    unittest.main()
