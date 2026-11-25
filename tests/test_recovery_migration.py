from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from binning_engine.config_migration import (
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_v2_snapshot,
)


FIXTURES = ROOT / "fixtures"


class MigrationRecoveryTests(unittest.TestCase):
    def test_v1_v2_time_mapping_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            v1_output = temporary / "roundtrip.v1.json"
            v2_output = temporary / "roundtrip.v2"
            migrate_v2_to_v1(FIXTURES / "config" / "binning_config.v2", v1_output)
            v1 = json.loads(v1_output.read_text())
            self.assertEqual(v1["recorded_at_relative_seconds"], 120.0)
            migrate_v1_to_v2(v1_output, v2_output)
            _, metadata = read_v2_snapshot(v2_output)
            self.assertEqual(metadata["absolute_ms"], 1700000120000)

    def test_sigkill_recovery_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline"
            recovered = root / "recovered"
            config = FIXTURES / "config" / "engine_config.json"
            samples = FIXTURES / "samples.jsonl"
            baseline_code = f"""
from binning_engine.pipeline import run_pipeline
run_pipeline({str(config)!r}, samples_path={str(samples)!r},
             output_dir={str(baseline)!r}, workers=2, batch_size=24)
"""
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            baseline_proc = subprocess.run(
                [sys.executable, "-c", baseline_code],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(baseline_proc.returncode, 0, baseline_proc.stderr)

            kill_code = baseline_code.replace(
                f"output_dir={str(baseline)!r}",
                f"output_dir={str(recovered)!r}, kill_after_batch=2",
            )
            kill_proc = subprocess.run(
                [sys.executable, "-c", kill_code],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(kill_proc.returncode, 0)

            resume_code = f"""
from binning_engine.pipeline import run_pipeline
result = run_pipeline({str(config)!r}, samples_path={str(samples)!r},
                      output_dir={str(recovered)!r}, workers=2, batch_size=24)
assert len(result['recovered_batches']) == 2
"""
            resume_proc = subprocess.run(
                [sys.executable, "-c", resume_code],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(resume_proc.returncode, 0, resume_proc.stderr)

            for name in ("encoding_result.json", "statistics.json"):
                self.assertEqual(
                    (baseline / name).read_bytes(),
                    (recovered / name).read_bytes(),
                    name,
                )
            baseline_checkpoint = next((baseline / "checkpoints").glob("*.json"))
            recovered_checkpoint = next((recovered / "checkpoints").glob("*.json"))
            self.assertEqual(
                baseline_checkpoint.read_bytes(),
                recovered_checkpoint.read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
