from __future__ import annotations

import shutil
import unittest

import numpy as np

from spkvc.cache import (
    feature_key_components,
    params_key_components,
    timbre_key_components,
)
from spkvc.config import PipelineConfig
from spkvc.dataset import Dataset, read_wav, write_wav
from spkvc.pipeline import ConversionEngine

from .support import DatasetTestCase


def _pcm_hashes(result) -> dict[str, str]:
    return result.summary()["pcm_sha256"]


class CacheAndIncrementalTests(DatasetTestCase):
    def test_cache_keys_change_only_for_relevant_inputs(self):
        entry = self.dataset.load_entries()[0]
        config = PipelineConfig()
        pcm, audio_hash = read_wav(
            self.dataset.utterance_audio_path(entry.utterance_id)
        )
        _, ref_hash_1 = read_wav(
            self.dataset.ref_audio_path(entry.ref_id, 1)
        )
        _, ref_hash_2 = read_wav(
            self.dataset.ref_audio_path(entry.ref_id, 2)
        )
        revised = entry.with_ref_revision(2)
        feature_1 = feature_key_components(entry, audio_hash, config)
        feature_2 = feature_key_components(revised, audio_hash, config)
        self.assertEqual(feature_1, feature_2)
        self.assertEqual(
            feature_1["ref_revision_policy"],
            "excluded_because_reference_independent",
        )
        timbre_1 = timbre_key_components(
            entry, ref_audio_chunk_sha256=ref_hash_1, config=config
        )
        timbre_2 = timbre_key_components(
            revised, ref_audio_chunk_sha256=ref_hash_2, config=config
        )
        self.assertNotEqual(timbre_1, timbre_2)
        upstream = {
            "frame_features": "a" * 64,
            "timbre_embedding": "b" * 64,
            "phoneme_alignment": "c" * 64,
        }
        mapping_v2 = PipelineConfig.from_dict(
            {
                **config.to_dict(),
                "mapping_version": "map-v2",
                "mapping_params": {
                    **config.mapping_params,
                    "f0_strength": 0.5,
                },
            }
        )
        params_1 = params_key_components(
            entry,
            audio_chunk_sha256=audio_hash,
            ref_audio_chunk_sha256=ref_hash_1,
            upstream_hashes=upstream,
            config=config,
        )
        params_2 = params_key_components(
            entry,
            audio_chunk_sha256=audio_hash,
            ref_audio_chunk_sha256=ref_hash_1,
            upstream_hashes=upstream,
            config=mapping_v2,
        )
        self.assertNotEqual(params_1, params_2)
        self.assertEqual(
            params_1["upstream_artifact_hashes"], upstream
        )

    def test_content_cache_reuse_query_and_complete_rerun(self):
        engine = self.engine("cache-baseline")
        first = engine.run()
        self.assertEqual(first.cache_hits, 4)
        self.assertEqual(first.cache_misses, 28)
        report = engine.cache_report("utt-001")
        self.assertTrue(report["reuse_check"]["keys_present"])
        self.assertTrue(report["reuse_check"]["artifact_hashes_present"])
        self.assertEqual(set(report["nodes"]), {
            "features", "timbre", "params", "synthesis"
        })
        self.assertIn("alignment", report["derived_nodes"])
        self.assertIn("upstream_artifact_hashes", report["nodes"]["params"]["key_components"])
        for record in report["nodes"].values():
            self.assertEqual(len(record["key"]), 64)
            self.assertEqual(len(record["artifact_hash"]), 64)

        second = engine.run()
        self.assertEqual(second.executed_stages, [])
        self.assertEqual(second.resumed_from_stage, 5)
        self.assertEqual(second.recomputed_nodes, [])
        self.assertEqual(_pcm_hashes(first), _pcm_hashes(second))

    def test_mapping_increment_recomputes_only_params_and_synthesis(self):
        engine = self.engine("mapping-increment")
        baseline = engine.run()
        value = engine.config.to_dict()
        value["mapping_version"] = "map-v2"
        value["mapping_params"] = {
            **value["mapping_params"],
            "f0_strength": 0.55,
        }
        new_config = PipelineConfig.from_dict(value)
        result = engine.incremental(new_config=new_config)
        self.assertEqual(
            set(result.affected_utterance_ids),
            {entry.utterance_id for entry in self.dataset.load_entries()},
        )
        self.assertTrue(
            all(
                node.startswith(("params:", "synthesis:"))
                for node in result.affected_nodes
            )
        )
        self.assertEqual(
            {node.split(":", 1)[0] for node in result.recomputed_nodes},
            {"params", "synthesis"},
        )
        for stage in ("features", "timbre", "alignment"):
            self.assertTrue(
                any(node.startswith(stage + ":") for node in result.reused_nodes)
            )
        self.assertFalse(set(result.recomputed_nodes) & set(result.reused_nodes))
        self.assertTrue(set(result.affected_nodes).issubset(
            set(result.recomputed_nodes) | set(result.reused_nodes)
        ))
        self.assertTrue(engine.self_check(result)["ok"])
        self.assertNotEqual(
            baseline.summary()["pcm_sha256"]["utt-001"],
            result.summary()["pcm_sha256"]["utt-001"],
        )
        self.assertTrue(all(version > 5 for version in result.snapshot_versions.values()))

    def test_reference_increment_affects_only_matching_reference_utterances(self):
        engine = self.engine("reference-increment")
        baseline = engine.run()
        entries = self.dataset.load_entries()
        expected = {
            entry.utterance_id
            for entry in entries
            if entry.ref_id == "ref-spk-02"
        }
        result = engine.incremental(ref_revisions={"ref-spk-02": 2})
        self.assertEqual(set(result.affected_utterance_ids), expected)
        self.assertFalse(
            any(node.startswith("features:") for node in result.affected_nodes)
        )
        self.assertTrue(
            all(
                node.split(":", 1)[1] in expected
                for node in result.affected_nodes
            )
        )
        recomputed_stages = {
            node.split(":", 1)[0] for node in result.recomputed_nodes
        }
        self.assertNotIn("features", recomputed_stages)
        self.assertTrue({"alignment", "params", "synthesis"}.issubset(recomputed_stages))
        self.assertFalse(set(result.recomputed_nodes) & set(result.reused_nodes))
        self.assertTrue(engine.self_check(result)["ok"])
        after = result.summary()["pcm_sha256"]
        before = baseline.summary()["pcm_sha256"]
        self.assertNotEqual(before["utt-001"], after["utt-001"])
        self.assertEqual(before["utt-002"], after["utt-002"])

    def test_reference_content_change_without_revision_bump_invalidates_downstream(self):
        dataset_root = self.tmp / "same-revision-data"
        shutil.copytree(self.dataset_root, dataset_root)
        dataset = Dataset(dataset_root)
        engine = ConversionEngine(
            dataset=dataset,
            state_dir=self.tmp / "same-revision-state",
            output_dir=self.tmp / "same-revision-state" / "outputs",
        )
        baseline = engine.run()
        ref_path = dataset.ref_audio_path("ref-spk-02", 1)
        pcm, _ = read_wav(ref_path)
        changed = (-pcm.astype(np.int32) // 2).astype(np.int16)
        write_wav(ref_path, changed, 16000)
        fresh = ConversionEngine(
            dataset=dataset,
            state_dir=self.tmp / "same-revision-state",
            output_dir=self.tmp / "same-revision-state" / "outputs",
        )
        result = fresh.incremental()
        self.assertEqual(
            set(result.affected_utterance_ids), {"utt-001", "utt-008"}
        )
        self.assertFalse(
            any(node.startswith("features:") for node in result.recomputed_nodes)
        )
        self.assertNotEqual(
            baseline.summary()["pcm_sha256"]["utt-001"],
            result.summary()["pcm_sha256"]["utt-001"],
        )


if __name__ == "__main__":
    unittest.main()

