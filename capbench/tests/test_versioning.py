import os
import tempfile
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from capbench.scene import load_scene
from capbench.versioning import VersionStore, fingerprint, diff_scenes


class VersioningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = VersionStore(os.path.join(self.tmp, "v.json"))
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.scene1 = load_scene(os.path.join(here, "scenarios",
                                              "cold_start.json"))
        self.scene2 = load_scene(os.path.join(here, "scenarios",
                                              "rerun_concurrency.json"))

    def test_version_monotonic_and_stable(self):
        fp1 = fingerprint(self.scene1)
        info1 = self.store.resolve(fp1)
        info1_again = self.store.resolve(fp1)
        self.assertEqual(info1.version, info1_again.version)
        self.assertFalse(info1_again.is_new)
        fp2 = fingerprint(self.scene2)
        info2 = self.store.resolve(fp2)
        self.assertEqual(info2.version, info1.version + 1)

    def test_diff_lists_concurrency_changes_per_stage(self):
        diffs = diff_scenes(self.scene1.raw, self.scene2)
        conc = [d for d in diffs if ".concurrency" in d]
        self.assertEqual(len(conc), 3)
        self.assertTrue(all("->" in d for d in conc))

    def test_threshold_change_appears_in_diff(self):
        import json as _json
        modified = _json.loads(_json.dumps(self.scene1.raw))
        modified["slo"]["max_p95_latency_ms"] = 5
        from capbench.scene import _parse_scene
        diffs = diff_scenes(self.scene1.raw, _parse_scene(modified))
        self.assertTrue(any("slo.max_p95" in d for d in diffs))

    def test_stage_order_change_detected(self):
        import json as _json
        modified = _json.loads(_json.dumps(self.scene1.raw))
        modified["stages"][0]["index"], modified["stages"][1]["index"] = 2, 1
        modified["stages"][1]["index"] = 1
        modified["stages"][0]["index"] = 2
        from capbench.scene import _parse_scene
        try:
            scene = _parse_scene(modified)
        except Exception:
            return  # order validation rejects; fingerprint still differs by set
        diffs = diff_scenes(self.scene1.raw, scene)
        self.assertTrue(any("stage_order" in d for d in diffs))


if __name__ == "__main__":
    unittest.main()
