import os
import shutil
import tempfile
import unittest

from ocrbench.cache import ContentCache
from ocrbench.config import GRAPH_NODES, PipelineConfig
from ocrbench.deterministic import content_hash
from ocrbench.errors import ErrorCode, OcrBenchError
from ocrbench.incremental import ChangeSet
from ocrbench.workbench import OcrWorkbench


def _valid_details(**overrides):
    details = {
        "node": "match",
        "page_id": "p1",
        "pixel_hash": "a" * 64,
        "config_hash": "b" * 64,
        "upstream_hash": "c" * 64,
        "charset_version": 1,
        "line_box_summary": "d" * 16,
    }
    details.update(overrides)
    return details


class CacheKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = ContentCache(os.path.join(self.tmp, "cache"), max_entries=100)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_components_raise_invalid_key(self):
        with self.assertRaises(OcrBenchError) as ctx:
            self.cache.build_key("match", {"node": "match", "page_id": "p1"})
        self.assertEqual(ctx.exception.code, ErrorCode.CACHE_KEY_INVALID)
        missing = ctx.exception.details["missing_fields"]
        for required in ("pixel_hash", "config_hash", "upstream_hash", "charset_version"):
            self.assertIn(required, missing)

    def test_commit_then_hit_returns_same_entry(self):
        details = _valid_details()
        payload = {"text": "ABC"}
        first = self.cache.commit("match", details, payload)
        second = self.cache.commit("match", dict(details), {"text": "ABC"})
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.content_hash, second.content_hash)
        fetched = self.cache.get(first.key)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.content_hash, content_hash(payload))

    def test_corrupt_payload_detection(self):
        details = _valid_details()
        entry = self.cache.commit("match", details, {"text": "ABC"})
        import json

        path = self.cache._path_for(entry.key)
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
        record["payload"]["text"] = "XYZ"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        with self.assertRaises(OcrBenchError) as ctx:
            self.cache.get(entry.key)
        self.assertEqual(ctx.exception.code, ErrorCode.CACHE_CORRUPT)

    def test_key_details_export_roundtrip(self):
        details = _valid_details()
        entry = self.cache.commit("match", details, {"v": 1})
        exported = self.cache.key_details(entry.key)
        self.assertEqual(exported["charset_version"], 1)
        self.assertEqual(self.cache.build_key("match", exported), entry.key)


class IncrementalInvalidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.wb = OcrWorkbench(os.path.join(self.tmp, "wb"), PipelineConfig())
        self.wb.create_dataset(
            [("page-001", 101), ("page-002", 202), ("page-003", 303)],
            charset_version=1,
        )
        self.baseline = self.wb.run_all()
        self.baseline_sig = {
            pid: {
                node: content_hash(result.payloads[node])
                for node in GRAPH_NODES
            }
            for pid, result in self.baseline.items()
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_only_new_page_recomputes(self):
        self.wb.append_page("page-004", 404)
        plan = self.wb.plan_incremental(ChangeSet(new_page_ids={"page-004"}))
        self.assertEqual(plan.affected_pages, {"page-004"})
        results = self.wb.run_all()
        self.assertEqual(set(results["page-004"].recomputed_nodes()), set(GRAPH_NODES))
        for pid in ("page-001", "page-002", "page-003"):
            self.assertEqual(results[pid].recomputed_nodes(), [])
            for node in GRAPH_NODES:
                self.assertEqual(
                    content_hash(results[pid].payloads[node]),
                    self.baseline_sig[pid][node],
                )

    def test_truth_correction_only_recomputes_aggregate(self):
        truth = self.baseline["page-001"].payloads["aggregate"]["per_line"][0]["truth"]
        self.wb.correct_truth("page-001", 0, "Q" + truth[1:])
        results = self.wb.run_all()
        self.assertEqual(results["page-001"].recomputed_nodes(), ["aggregate"])
        # Recognize output stays byte identical even though metrics change.
        self.assertEqual(
            content_hash(results["page-001"].payloads["recognize"]),
            self.baseline_sig["page-001"]["recognize"],
        )
        self.assertNotEqual(
            content_hash(results["page-001"].payloads["aggregate"]),
            self.baseline_sig["page-001"]["aggregate"],
        )

    def test_threshold_change_invalidates_pixel_suffix(self):
        bumped_config = PipelineConfig(detect_threshold=55)
        wb2 = OcrWorkbench(os.path.join(self.tmp, "wb2"), bumped_config)
        wb2.cache = self.wb.cache
        wb2.engine.cache = self.wb.cache
        wb2._pages = dict(self.wb._pages)
        plan = wb2.plan_incremental(ChangeSet(detect_threshold_changed=True))
        self.assertEqual(plan.reused_nodes, [])
        results = wb2.run_all()
        for result in results.values():
            self.assertEqual(set(result.recomputed_nodes()), set(GRAPH_NODES))


if __name__ == "__main__":
    unittest.main()
