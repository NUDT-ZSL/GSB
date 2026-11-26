from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ifdata.engine import PipelineEngine
from ifdata.errors import BusinessError, E_WORKER_FAILURE
from ifdata.generator import generate_fixture
from ifdata.utils import read_json, sha256_file


class EndToEndTests(unittest.TestCase):
    def test_six_stage_v1_v2_consistency_and_traceability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=32)
            v2_engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state-v2",
                out_dir=root / "out-v2",
                strategy_generation=2,
            )
            v2_report = v2_engine.run()
            v1_engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state-v1",
                out_dir=root / "out-v1",
                strategy_generation=1,
            )
            v1_report = v1_engine.run()
            self.assertEqual(v2_report["status"], "complete")
            self.assertEqual(v1_report["status"], "complete")
            self.assertEqual(
                [
                    item["snapshot_version"]
                    for item in v2_report["state"]["committed_stages"]
                ],
                [1, 2, 3, 4, 5, 6],
            )
            v2_dataset = root / "out-v2/dataset.jsonl"
            v1_dataset = root / "out-v1/dataset.jsonl"
            self.assertEqual(v2_dataset.read_bytes(), v1_dataset.read_bytes())
            rows = [
                json.loads(line)
                for line in v2_dataset.read_text(encoding="utf-8").splitlines()
                if line
            ]
            manifest = read_json(root / "out-v2/export_manifest.json")
            self.assertEqual([row["sample_id"] for row in rows], manifest["sample_ids"])
            for row in rows:
                trace = row["trace"]
                self.assertIn("raw_line_no", trace)
                self.assertIn("raw_sample_checksum", trace)
                self.assertIn("sampling_target_id", trace)
                self.assertIn("sampling_seed", trace)
                self.assertIn("quality_contributions", trace)
                self.assertIn("cluster_id", trace)
            dedup = v2_engine.show_stage("dedup")["payload"]
            self.assertTrue(
                dedup["summary"]["all_samples_in_exactly_one_cluster"]
            )
            sampling = v2_engine.show_stage("sampling")["payload"]
            expected_order = [
                entry["sample"]["sample_id"] for entry in sampling["selected"]
            ]
            self.assertEqual(expected_order, manifest["sample_ids"])
            self.assertEqual(
                sha256_file(v2_dataset), manifest["dataset_checksum"]
            )

    def test_worker_failure_returns_code_stage_and_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state",
                out_dir=root / "out",
            )
            with self.assertRaises(BusinessError) as context:
                engine.run(failpoints={"quality:1": "forced worker error"})
            exc = context.exception
            self.assertEqual(exc.code, E_WORKER_FAILURE)
            self.assertEqual(exc.stage, "quality")
            self.assertEqual(exc.batch_id, "quality-batch-0001")
            self.assertEqual(
                [item["stage"] for item in engine.store.load_snapshots()],
                ["ingest", "template"],
            )


if __name__ == "__main__":
    unittest.main()

