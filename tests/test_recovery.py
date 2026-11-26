from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from diagnostic_engine.checkpoint import CheckpointStore, ForcedTermination
from diagnostic_engine.engine import run_engine
from diagnostic_engine.generator import generate_dataset


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.generated = generate_dataset(cls.root / "data", sample_count=96)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_forced_termination_resumes_without_recomputing_committed_steps(self) -> None:
        output = self.root / "recover"
        with self.assertRaises(ForcedTermination) as context:
            run_engine(
                data_path=self.generated.samples_path,
                snapshot_path=self.generated.v2_path,
                output_directory=output,
                precisions=("float64", "float32", "fixed"),
                abort_precision="float64",
                abort_after_steps=2,
            )
        self.assertEqual(context.exception.committed_steps, 2)
        terminated = json.loads(
            (output / "terminated.json").read_text(encoding="utf-8")
        )
        self.assertEqual(terminated["forced_termination"]["code"], "E_FORCED_TERMINATION")
        store = CheckpointStore(output / "precisions" / "float64")
        self.assertGreaterEqual(store.latest_version(), 2)
        checkpoints = sorted((output / "precisions" / "float64").glob("cp-*.json"))
        self.assertEqual(len(checkpoints), 2)
        versions = [store.load_path(path).version for path in checkpoints]
        self.assertEqual(versions, sorted(versions))
        self.assertTrue(all(
            store.load_path(path).checksum for path in checkpoints
        ))
        resumed = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v2_path,
            output_directory=output,
            precisions=("float64", "float32", "fixed"),
        )
        resume = resumed["precisions"]["float64"]["resume"]
        self.assertEqual(resume["reused_steps"], 2)
        self.assertEqual(resume["recomputed_steps"], 3)
        self.assertEqual(resumed["validation"]["status"], "passed")
        final = run_engine(
            data_path=self.generated.samples_path,
            snapshot_path=self.generated.v2_path,
            output_directory=output,
            precisions=("float64", "float32", "fixed"),
        )
        for precision in ("float64", "float32", "fixed"):
            self.assertEqual(
                final["precisions"][precision]["resume"]["reused_steps"], 5
            )
            self.assertEqual(
                final["precisions"][precision]["resume"]["recomputed_steps"], 0
            )


if __name__ == "__main__":
    unittest.main()

