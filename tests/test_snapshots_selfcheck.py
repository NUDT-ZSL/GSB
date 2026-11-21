"""快照版本化、并发控制、资源上限与自检测试。"""

import threading
import unittest

from detwb.dag import Pipeline
from detwb.errors import Err, WorkbenchError
from detwb.samples import gen_manifest
from detwb.selfcheck import run_selfcheck
from detwb.snapshots import SnapshotStore
from detwb.vocab import VocabRegistry
from tests.helpers import make_vocab


def _registry():
    reg = VocabRegistry()
    reg.load(make_vocab())
    return reg


def _config(**limits):
    base = {"max_images": 1000, "max_iou_comparisons": 10000000,
            "max_resident_candidates": 100000, "degrade_allowed": False}
    base.update(limits)
    return {"iou_threshold": 0.5, "fallback_class_id": None, "limits": base}


class TestSnapshots(unittest.TestCase):
    def test_version_monotonic_and_readback(self):
        store = SnapshotStore()
        s1 = store.commit("k", {"a": 1})
        s2 = store.commit("k", {"a": 2})
        self.assertEqual((s1.version, s2.version), (1, 2))
        self.assertEqual(store.read("k").payload, {"a": 2})

    def test_corrupt_detected(self):
        store = SnapshotStore()
        store.commit("k", {"a": 1})
        store.corrupt_for_test("k")
        with self.assertRaises(WorkbenchError) as ctx:
            store.read("k")
        self.assertEqual(ctx.exception.code, Err.CACHE_CORRUPT)

    def test_concurrent_commit_busy(self):
        store = SnapshotStore()
        store._commit_lock.acquire()
        errors = []

        def late_commit():
            try:
                store.commit("k", {"a": 1})
            except WorkbenchError as err:
                errors.append(err.code)

        t = threading.Thread(target=late_commit)
        t.start()
        t.join(timeout=5)
        store._commit_lock.release()
        self.assertEqual(errors, [Err.STAGE_BUSY])

    def test_store_roundtrip_and_tamper_on_load(self):
        store = SnapshotStore()
        store.commit("k", {"a": 1})
        data = store.to_dict()
        restored = SnapshotStore.from_dict(data)
        self.assertEqual(restored.read("k").payload, {"a": 1})
        data["k"]["payload"]["a"] = 999
        with self.assertRaises(WorkbenchError) as ctx:
            SnapshotStore.from_dict(data)
        self.assertEqual(ctx.exception.code, Err.CACHE_CORRUPT)


class TestResourceLimits(unittest.TestCase):
    def test_images_limit(self):
        samples = gen_manifest(777, 6, 3)
        pipe = Pipeline(_config(max_images=3), _registry())
        with self.assertRaises(WorkbenchError) as ctx:
            pipe.run(samples)
        self.assertEqual(ctx.exception.code, Err.RESOURCE_LIMIT)
        self.assertEqual(ctx.exception.category, "images")

    def test_resident_limit_strict_and_degraded(self):
        samples = gen_manifest(777, 6, 3)
        pipe = Pipeline(_config(max_resident_candidates=5), _registry())
        with self.assertRaises(WorkbenchError) as ctx:
            pipe.run(samples)
        self.assertEqual(ctx.exception.category, "resident_candidates")
        pipe2 = Pipeline(_config(max_resident_candidates=5,
                                 degrade_allowed=True), _registry())
        result = pipe2.run(samples)
        self.assertEqual(result["degradation"], ["DEGRADE_CHUNKED_RESIDENT"])

    def test_iou_comparisons_limit(self):
        samples = gen_manifest(777, 6, 3)
        pipe = Pipeline(_config(max_iou_comparisons=1), _registry())
        with self.assertRaises(WorkbenchError) as ctx:
            pipe.run(samples)
        self.assertEqual(ctx.exception.category, "iou_comparisons")


class TestSelfCheck(unittest.TestCase):
    def test_selfcheck_passes_and_detects_tamper(self):
        samples = gen_manifest(777, 6, 3)
        pipe = Pipeline(_config(), _registry())
        result = pipe.run(samples)
        samples_gt = {s["sample_id"]: s["gt"] for s in samples}
        passed = run_selfcheck(result, samples_gt, pipe.store)
        self.assertEqual(len(passed), 4)
        sid = sorted(result["stats"])[0]
        if result["stats"][sid]["tp"]:
            result["stats"][sid]["fp"].append(result["stats"][sid]["tp"][0])
            with self.assertRaises(WorkbenchError) as ctx:
                run_selfcheck(result, samples_gt, pipe.store)
            self.assertEqual(ctx.exception.code, Err.INCONSISTENT)
            self.assertEqual(ctx.exception.category, "cand_tp_fp_overlap")


if __name__ == "__main__":
    unittest.main()
