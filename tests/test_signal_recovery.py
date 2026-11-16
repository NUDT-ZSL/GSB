import json
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from panseg.errors import E_HARD_CRASH_SIMULATED, PipelineError
from panseg.models import PipelineConfig
from panseg.pipeline import PanopticPipeline
from panseg.rle import bitmap_to_runs
from panseg.v1_package import V1RawInstance, write_v1_package
from panseg.v2_package import V2RawInstance, write_v2_package


class SignalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sample = self.root / "sample"
        self.sample.mkdir()
        bitmap = np.zeros((8, 8), dtype=np.uint8)
        bitmap[1:6, 1:6] = 1
        runs, pixels = bitmap_to_runs(bitmap)
        write_v1_package(
            self.sample / "scene-00.v1.bin",
            8,
            8,
            [V1RawInstance("scene-00-inst-00", "car", runs, pixels)],
            1,
        )
        write_v2_package(
            self.sample / "scene-00.v2.json",
            8,
            8,
            [V2RawInstance("scene-00-inst-01", "car", runs, pixels, 0.8, False, "c", (1, 2, 3))],
            {"c": {"rgb": [1, 2, 3]}},
        )
        (self.sample / "metadata.json").write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "instance_id": "scene-00-inst-00",
                            "semantic_class": "car",
                            "confidence": 0.7,
                            "is_crowd": False,
                        }
                    ]
                }
            )
        )
        self.config = PipelineConfig(
            workers=2,
            rle_compression=1,
            retention_policy="keep_all",
            min_coverage=0.01,
            iou_threshold=0.3,
            default_confidence=0.5,
            default_crowd=False,
            snapshot_commit_interval=1,
            boundary_iou_threshold=0.7,
            fault_injection={},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_hard_crash_leaves_no_committed_snapshot_and_recovers(self):
        work = self.root / "work-crash"
        crash_config = PipelineConfig(
            **{
                **self.config.to_dict(),
                "fault_injection": {
                    "snapshot_hard_crash": {"after_instance": "scene-00-inst-00"}
                },
            }
        )
        pipeline = PanopticPipeline(self.sample, crash_config, work)
        with self.assertRaises(PipelineError) as ctx:
            pipeline.run()
        self.assertEqual(ctx.exception.code, E_HARD_CRASH_SIMULATED)
        # No complete snapshot may be visible after a simulated hard crash.
        self.assertIsNone(pipeline.store.current_version())

        # Recovery from the next clean run must succeed from an empty state.
        clean = PanopticPipeline(self.sample, self.config, work)
        result = clean.run()
        self.assertEqual(result.stats.snapshot_version, 1)
        self.assertEqual(clean.store.current_version(), 1)

    def test_shutdown_flag_is_honored_in_report_contract(self):
        work = self.root / "work-signal"
        pipeline = PanopticPipeline(self.sample, self.config, work)
        pipeline.request_shutdown()
        self.assertTrue(pipeline.signaled)
        result = pipeline.run()
        self.assertTrue(result.signaled)


if __name__ == "__main__":
    unittest.main()
