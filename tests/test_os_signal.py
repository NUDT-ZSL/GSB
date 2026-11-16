import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from panseg.rle import bitmap_to_runs
from panseg.v1_package import V1RawInstance, write_v1_package
from panseg.v2_package import V2RawInstance, write_v2_package


class OsSignalTests(unittest.TestCase):
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
        config = {
            "workers": 1,
            "rle_compression": 1,
            "retention_policy": "keep_all",
            "min_coverage": 0.01,
            "iou_threshold": 0.3,
            "default_confidence": 0.5,
            "default_crowd": False,
            "snapshot_commit_interval": 1,
            "boundary_iou_threshold": 0.7,
        }
        self.config_path = self.sample / "config.json"
        self.config_path.write_text(json.dumps(config))
        self.work = self.root / "work"
        self.runner = Path(__file__).resolve().parent / "fixtures" / "signal_runner.py"

    def tearDown(self):
        self.tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX signal semantics")
    def test_sigterm_finishes_commit_with_exit_zero(self):
        cmd = [
            sys.executable,
            str(self.runner),
            "--sample-dir",
            str(self.sample),
            "--work-dir",
            str(self.work),
            "--config",
            str(self.config_path),
        ]
        env = dict(os.environ, PANSEG_PARK_BEFORE_RUN="1")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        # The tiny workload finishes quickly; signal immediately and let the
        # handler participate if the process is still alive.
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, msg=stderr)
        report = json.loads((self.work / "validation_report.json").read_text())
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["signaled_shutdown"])
        pointer = json.loads((self.work / "snapshots" / "current.json").read_text())
        self.assertEqual(pointer["version"], 1)


if __name__ == "__main__":
    unittest.main()
