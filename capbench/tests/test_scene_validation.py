import json
import os
import tempfile
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from capbench.errors import SceneError
from capbench.scene import load_scene

VALID = {
    "name": "t",
    "target": {"host": "127.0.0.1", "port": 8431},
    "sample_interval_ms": 250,
    "slo": {"min_success_rate_bp": 9900, "max_p95_latency_ms": 80},
    "stages": [
        {"index": 1, "duration_s": 2, "concurrency": 2,
         "throughput_limit_rps": 20},
        {"index": 2, "duration_s": 2, "concurrency": 4,
         "throughput_limit_rps": 30},
    ],
}


class SceneValidationTest(unittest.TestCase):
    def _write(self, payload) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        return path

    def test_valid_scene_loads(self):
        scene = load_scene(self._write(VALID))
        self.assertEqual(len(scene.stages), 2)
        self.assertEqual(scene.slo.min_success_rate_bp, 9900)

    def test_corrupt_json_rejected_with_escene(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            fh.write("{broken")
        with self.assertRaises(SceneError) as ctx:
            load_scene(path)
        self.assertEqual(ctx.exception.code, "E_SCENE")

    def test_all_field_paths_reported_and_whole_load_rejected(self):
        bad = json.loads(json.dumps(VALID))
        bad["target"] = {"host": "8.8.8.8", "port": 70000}
        bad["slo"] = {"min_success_rate_bp": 12000, "max_p95_latency_ms": 0}
        bad["stages"] = [
            {"index": 2, "duration_s": 0, "concurrency": -1,
             "throughput_limit_rps": 0},
            {"index": 2, "duration_s": 1, "concurrency": 1,
             "throughput_limit_rps": 1},
        ]
        with self.assertRaises(SceneError) as ctx:
            load_scene(self._write(bad))
        paths = ctx.exception.field_paths
        for expected in ("target.host", "target.port",
                         "slo.min_success_rate_bp", "slo.max_p95_latency_ms",
                         "stages[0].duration_s", "stages[0].concurrency",
                         "stages[0].throughput_limit_rps",
                         "stages[1].index(duplicate)"):
            self.assertIn(expected, paths)

    def test_non_consecutive_indexes_rejected(self):
        bad = json.loads(json.dumps(VALID))
        bad["stages"][1]["index"] = 3
        with self.assertRaises(SceneError) as ctx:
            load_scene(self._write(bad))
        self.assertTrue(
            any("not_consecutive" in p for p in ctx.exception.field_paths))

    def test_booleans_not_accepted_as_ints(self):
        bad = json.loads(json.dumps(VALID))
        bad["stages"][0]["concurrency"] = True
        with self.assertRaises(SceneError) as ctx:
            load_scene(self._write(bad))
        self.assertIn("stages[0].concurrency", ctx.exception.field_paths)


if __name__ == "__main__":
    unittest.main()
