from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voice_state.canonical import read_json, write_jsonl_atomic
from voice_state.config import AnalysisConfig
from voice_state.dataset import load_dataset
from voice_state.embeddings import load_or_build_embedding_cache
from voice_state.engine import AnalysisEngine
from voice_state.errors import (
    BusinessError,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from voice_state.snapshot import SnapshotStore
from voice_state.synth import append_registry_sample

from tests.helpers import make_dataset


class SnapshotContractTests(unittest.TestCase):
    def test_busy_lock_returns_immediately_and_rollback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = SnapshotStore(Path(temp) / "state")
            store.initialize_or_reconcile(
                analysis_key="abc",
                registry_version=1,
                registry_checksum="registry-checksum",
            )
            with store.hold_commit_lock("test"):
                with self.assertRaises(BusinessError) as caught:
                    store.commit_stage(
                        "framing",
                        {"dialog_ids": ["d1"]},
                        analysis_key="abc",
                        registry_version=1,
                        registry_checksum="registry-checksum",
                    )
                self.assertEqual(caught.exception.code, E_STAGE_BUSY)
            store.commit_stage(
                "framing",
                {"dialog_ids": ["d1"]},
                analysis_key="abc",
                registry_version=1,
                registry_checksum="registry-checksum",
            )
            current = store.load_manifest()["stages"]["framing"]
            with self.assertRaises(BusinessError) as caught:
                store.commit_stage(
                    "framing",
                    {"dialog_ids": ["d1"]},
                    analysis_key="abc",
                    registry_version=1,
                    registry_checksum="registry-checksum",
                    supplied_version=current["version"],
                )
            self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertIn("stage", caught.exception.details)

    def test_corrupt_snapshot_reports_stage_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=1, seed=41)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            engine.run()
            record = engine.store.load_manifest()["stages"]["segmentation"]
            path = engine.store.snapshots_dir / record["snapshot"]
            payload = read_json(path)
            payload["snapshot"]["payload"]["dialog_ids"] = ["tampered"]
            path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
            with self.assertRaises(BusinessError) as caught:
                engine.store.load_stage("segmentation")
            self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(caught.exception.details["stage"], "segmentation")
            self.assertEqual(caught.exception.details["version"], record["version"])

    def test_registry_version_change_replaces_prefix_and_reuses_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=2, seed=42)
            engine = AnalysisEngine(dataset_dir, Path(temp) / "state")
            first = engine.run()
            before = engine.status()
            reused = engine.run()
            self.assertEqual(reused.computed_stages, [])
            self.assertEqual(first.report_bytes, reused.report_bytes)

            append_registry_sample(
                dataset_dir / "registrations.jsonl",
                speaker_id=0,
                clip_seed=777,
                clip_ms=800,
            )
            changed = AnalysisEngine(dataset_dir, engine.state_dir)
            status = changed.status()
            self.assertEqual(status["registry_version"], 2)
            self.assertEqual(status["generation"], before["generation"])
            self.assertEqual(status["epoch"], before["epoch"] + 1)
            self.assertTrue(all(record["status"] == "pending" for record in status["stages"].values()))
            result = changed.run()
            self.assertEqual(result.computed_stages, ["framing", "segmentation", "regression", "aggregation"])
            versions = [result.status["stages"][stage]["version"] for stage in ("framing", "segmentation", "regression", "aggregation")]
            self.assertTrue(all(left < right for left, right in zip(versions, versions[1:])))
            self.assertTrue(all(version > before["generation"] for version in versions))
            self.assertEqual(result.report["registry_version"], 2)
            archive_dirs = list((engine.state_dir / "archive").iterdir())
            self.assertEqual(len(archive_dirs), 1)
            cache_files = sorted((engine.state_dir / "cache").glob("registry-v*.json"))
            self.assertEqual(len(cache_files), 1)
            self.assertTrue((archive_dirs[0] / "cache").is_dir())

    def test_cache_checksum_detects_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=1, speakers=2, seed=43)
            dataset = load_dataset(dataset_dir)
            config = AnalysisConfig()
            cache_dir = Path(temp) / "cache"
            cache = load_or_build_embedding_cache(dataset, config, cache_dir)
            path = next(cache_dir.glob("registry-v*.json"))
            payload = read_json(path)
            payload["speakers"]["0"]["mean"][0] = 999.0
            path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
            with self.assertRaises(BusinessError) as caught:
                load_or_build_embedding_cache(dataset, config, cache_dir)
            self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)
            self.assertEqual(caught.exception.details["stage"], "segmentation")
            self.assertEqual(caught.exception.details["component"], "speaker_registry")
            self.assertEqual(caught.exception.details["version"], 1)

    def test_resource_limit_has_degradation_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dataset_dir = make_dataset(Path(temp), dialogs=2, speakers=2, seed=44)
            engine = AnalysisEngine(
                dataset_dir,
                Path(temp) / "state",
                AnalysisConfig(max_dialogs_per_stage=1),
            )
            with self.assertRaises(BusinessError) as caught:
                engine.run()
            self.assertEqual(caught.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(caught.exception.details["degradation_category"], "dialog_count")
            self.assertEqual(caught.exception.details["stage"], "framing")


if __name__ == "__main__":
    unittest.main()

