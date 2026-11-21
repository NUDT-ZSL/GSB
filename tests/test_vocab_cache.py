"""词表演进、缓存键与增量失效测试。"""

import unittest

from detwb.cache import node_cache_key, plan_invalidation
from detwb.errors import Err, WorkbenchError
from detwb.samples import gen_manifest
from detwb.vocab import VocabRegistry, VocabSnapshot
from tests.helpers import make_vocab


class TestVocab(unittest.TestCase):
    def test_rollback_rejected(self):
        reg = VocabRegistry()
        v1 = make_vocab()
        v2 = VocabSnapshot(2, {0: {"name": "person", "retired": False}})
        reg.load(v2)
        with self.assertRaises(WorkbenchError) as ctx:
            reg.load(v1)
        self.assertEqual(ctx.exception.code, Err.SNAPSHOT_ROLLBACK)

    def test_wholesale_replace_on_change(self):
        reg = VocabRegistry()
        v1 = make_vocab()
        reg.load(v1)
        v2 = VocabSnapshot(2, {0: {"name": "person", "retired": False},
                               1: {"name": "car", "retired": True}})
        self.assertTrue(reg.load(v2))
        self.assertIs(reg.current, v2)
        self.assertFalse(reg.load(v2))

    def test_checksum_tamper_detected(self):
        data = make_vocab().to_dict()
        data["checksum"] = "0" * 64
        with self.assertRaises(WorkbenchError) as ctx:
            VocabSnapshot.from_dict(data)
        self.assertEqual(ctx.exception.code, Err.CACHE_CORRUPT)

    def test_retired_label_and_fallback(self):
        reg = VocabRegistry()
        reg.load(VocabSnapshot(2, {0: {"name": "person", "retired": False},
                                   3: {"name": "bicycle", "retired": True}}))
        with self.assertRaises(WorkbenchError) as ctx:
            reg.resolve_class(3)
        self.assertEqual(ctx.exception.code, Err.LABEL_RETIRED)
        cid, name, prov = reg.resolve_class(3, fallback_class_id=0)
        self.assertEqual((cid, name), (0, "person"))
        self.assertEqual(prov, "fallback:3->0")


class TestCacheKeys(unittest.TestCase):
    def test_key_inputs_matter(self):
        base = node_cache_key("assign", "s0", "img", "cand", "gt",
                              0.5, 1, "up")
        self.assertEqual(base, node_cache_key("assign", "s0", "img", "cand",
                                              "gt", 0.5, 1, "up"))
        self.assertNotEqual(base, node_cache_key("assign", "s0", "img", "cand",
                                                 "gt", 0.6, 1, "up"))
        self.assertNotEqual(base, node_cache_key("assign", "s0", "img", "cand",
                                                 "gt", 0.5, 2, "up"))
        self.assertNotEqual(base, node_cache_key("assign", "s0", "img2",
                                                 "cand", "gt", 0.5, 1, "up"))


class TestInvalidation(unittest.TestCase):
    def setUp(self):
        self.samples = gen_manifest(777, 6, 3)
        self.v1 = make_vocab()
        self.v2 = VocabSnapshot(2, {
            0: {"name": "person", "retired": False},
            1: {"name": "car", "retired": True},
            2: {"name": "dog", "retired": False},
        })

    def test_disjoint_and_covering(self):
        plan = plan_invalidation(self.samples, {"iou_threshold": 0.5},
                                 {"iou_threshold": 0.5}, self.v1, self.v2)
        self.assertFalse(set(plan["recompute"]) & set(plan["reuse"]))
        self.assertEqual(sorted(set(plan["recompute"]) | set(plan["reuse"])),
                         sorted(s["sample_id"] for s in self.samples))

    def test_threshold_change_affects_all(self):
        plan = plan_invalidation(self.samples, {"iou_threshold": 0.5},
                                 {"iou_threshold": 0.7}, self.v1, self.v1)
        self.assertEqual(len(plan["recompute"]), len(self.samples))
        self.assertEqual(plan["reuse"], [])

    def test_vocab_change_affects_only_referencing(self):
        plan = plan_invalidation(self.samples, {"iou_threshold": 0.5},
                                 {"iou_threshold": 0.5}, self.v1, self.v2)
        referencing = sorted(
            s["sample_id"] for s in self.samples
            if any(int(c["class_id"]) == 1 for c in s["candidates"])
            or any(int(g["label"]) == 1 for g in s["gt"]))
        self.assertEqual(plan["recompute"], referencing)

    def test_no_change_no_invalidation(self):
        plan = plan_invalidation(self.samples, {"iou_threshold": 0.5},
                                 {"iou_threshold": 0.5}, self.v1, self.v1)
        self.assertEqual(plan["recompute"], [])
        self.assertEqual(len(plan["reuse"]), len(self.samples))


if __name__ == "__main__":
    unittest.main()
