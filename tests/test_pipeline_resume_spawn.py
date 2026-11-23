from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from sparse_recall.pipeline import PipelineConfig, run_pipeline

from tests._fixtures import load_inputs, make_dataset, scheduler_config


ROOT = Path(__file__).resolve().parents[1]


def output_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((directory / "outputs").iterdir())
    }


class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary, cls.data = make_dataset()
        cls.weights, cls.ingested = load_inputs(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_spawn_pipeline_end_to_end(self) -> None:
        workdir = Path(self.temporary.name) / "spawn-run"
        result = run_pipeline(
            document_path=self.data / "documents.jsonl",
            query_path=self.data / "queries.jsonl",
            weights_path=self.data / "term_weights.json",
            workdir=workdir,
            scheduler_config=scheduler_config(
                self.ingested,
                fma_budget=26000,
                memory_budget=20000,
            ),
            config=PipelineConfig(
                workers=2,
                use_process_pool=True,
                compare_mode="tolerance",
                tolerance=1.0e-9,
            ),
        )
        self.assertEqual(result.report["status"], "ok")
        self.assertEqual(result.report["environment"]["process_pool"], "spawn")
        self.assertEqual(result.report["verification"]["status"], "ok")
        self.assertGreater(result.report["verification"]["checked_document_vectors"], 0)
        self.assertGreater(result.report["verification"]["checked_queries"], 0)

    def test_hard_exit_resume_matches_uninterrupted_outputs(self) -> None:
        base = Path(self.temporary.name)
        resumed = base / "resume-run"
        uninterrupted = base / "full-run"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT)

        common = [
            sys.executable,
            "-m",
            "sparse_recall",
            "run",
            "--serial",
            "--data-dir",
            str(self.data),
            "--fma-budget",
            "26000",
            "--memory-budget",
            "20000",
            "--threshold",
            "0.0",
            "--compare-mode",
            "tolerance",
        ]
        crashed = subprocess.run(
            common
            + [
                "--workdir",
                str(resumed),
                "--fresh",
                "--crash-after-commits",
                "17",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(crashed.returncode, 137, crashed.stderr)
        resumed_process = subprocess.run(
            common + ["--workdir", str(resumed)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(resumed_process.returncode, 0, resumed_process.stderr)
        full_process = subprocess.run(
            common + ["--workdir", str(uninterrupted), "--fresh"],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(full_process.returncode, 0, full_process.stderr)
        self.assertEqual(output_hashes(resumed), output_hashes(uninterrupted))
        recovery = json.loads((resumed / "recovery_report.json").read_text())
        self.assertTrue(recovery["resumed"])
        self.assertEqual(
            recovery["final_checkpoint_version"],
            json.loads(
                (uninterrupted / "checkpoints" / "checkpoint.json").read_text()
            )["version"],
        )

    def test_single_batch_failure_does_not_stop_other_batches(self) -> None:
        workdir = Path(self.temporary.name) / "failure-isolation"
        result = run_pipeline(
            document_path=self.data / "documents.jsonl",
            query_path=self.data / "queries.jsonl",
            weights_path=self.data / "term_weights.json",
            workdir=workdir,
            scheduler_config=scheduler_config(
                self.ingested,
                fma_budget=26000,
                memory_budget=20000,
            ),
            config=PipelineConfig(
                workers=2,
                use_process_pool=True,
                injected_failures={
                    "doc:1": {
                        "projection": {
                            "code": "E_WORKER",
                            "message": "intentional projection failure",
                        }
                    }
                },
            ),
        )
        self.assertEqual(result.report["status"], "ok")
        consistency = result.report["verification"]
        self.assertEqual(consistency["stage_batch_sets"]["projection"]["failed"], 1)
        self.assertGreater(
            consistency["stage_batch_sets"]["projection"]["completed"],
            0,
        )
        failures = (workdir / "outputs" / "failures.jsonl").read_text().splitlines()
        self.assertEqual(len(failures), 1)
        self.assertIn('"batch_key":"doc:1"', failures[0])
        self.assertTrue((workdir / "outputs" / "results.jsonl").stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
