from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from voiceprint_diag.audio import read_wav
from voiceprint_diag.config import load_config
from voiceprint_diag.features import extract_features
from voiceprint_diag.pipeline import run_pipeline
from voiceprint_diag.snapshots import ConfigMismatchError
from voiceprint_diag.synthesize import generate_demo, synthesise_voice, write_pcm16_wav


def _write_config(source: Path, destination: Path, **changes: object) -> Path:
    value = json.loads(source.read_text(encoding="utf-8"))
    value.update(changes)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _report(output: Path) -> dict:
    return json.loads((output / "report.json").read_text(encoding="utf-8"))


def _json_roundtrip(value: dict) -> dict:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(prefix="voiceprint-tests-")
        cls.root = Path(cls.temporary.name)
        cls.inputs = cls.root / "inputs"
        generate_demo(cls.inputs, seed=20261117)
        cls.samples = cls.inputs / "samples"
        cls.config = cls.inputs / "config.json"
        cls.pairs = cls.inputs / "pairs.json"
        cls.anomalies = cls.inputs / "anomalies.json"
        cls.baseline = cls.root / "baseline"
        outcome = run_pipeline(
            sample_dir=cls.samples,
            config_path=cls.config,
            pairs_path=cls.pairs,
            anomalies_path=cls.anomalies,
            output_dir=cls.baseline,
        )
        cls.baseline_outcome = outcome
        cls.baseline_report = _report(cls.baseline)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def _copy_baseline(self, name: str) -> Path:
        destination = self.root / name
        _copy_tree(self.baseline, destination)
        return destination

    def test_baseline_quality_and_anomaly_hits(self) -> None:
        deterministic = self.baseline_report["deterministic_results"]
        summary = deterministic["summary"]
        self.assertEqual(summary["same_speaker_pairs"], 3)
        self.assertEqual(summary["cross_speaker_pairs"], 3)
        self.assertEqual(summary["false_accepts"], 0)
        self.assertEqual(summary["false_rejects"], 0)
        self.assertEqual(summary["hit_anomaly_count"], 5)
        self.assertEqual(summary["declared_anomaly_count"], 5)
        codes = {item["code"] for item in deterministic["failures"]}
        self.assertIn("E_AUDIO_FORMAT", codes)
        self.assertIn("E_INSUFFICIENT_FRAMES", codes)
        self.assertEqual(deterministic["anomalies"]["hit_ids"], [f"INJ-RATE-001", "INJ-CHANNELS-001", "INJ-BITS-001", "INJ-TRUNC-001", "INJ-SILENCE-001"])
        for sample in deterministic["samples"]:
            if sample["status"] == "ok":
                self.assertGreater(sample["feature_summary"]["frame_count"], 0)
                self.assertEqual(sample["feature_summary"]["tail_frame_count"], 1)
                self.assertIn("embedding_fingerprint", sample["embedding"])
                self.assertGreater(sample["embedding"]["residual_abs_sum"], 0)
                self.assertIsNotNone(sample["aggregation"]["participating_frame_range"])

    def test_feature_repeatability_and_bad_audio_validation(self) -> None:
        config = load_config(self.config)
        audio = read_wav(self.samples / "speaker_a" / "a1.wav", self.samples, config)
        first = extract_features(audio, config)
        second = extract_features(audio, config)
        self.assertEqual(first.as_payload(), second.as_payload())
        self.assertTrue(first.frames[-1]["tail"])
        for relative in (
            "defects/bad_rate.wav",
            "defects/bad_channels.wav",
            "defects/bad_bits.wav",
            "defects/bad_truncated.wav",
        ):
            with self.assertRaises(ValueError):
                read_wav(self.samples / relative, self.samples, config)

    def test_noop_cache_reuse_and_concurrency_invariance(self) -> None:
        output = self._copy_baseline("concurrency-invariant")
        rerun = run_pipeline(
            sample_dir=self.samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        incremental = rerun.report["incremental"]
        self.assertEqual(incremental["reused_count"], 30)
        self.assertEqual(incremental["recomputed_count"], 0)
        self.assertEqual(incremental["invalidated_count"], 0)
        self.assertEqual(rerun.report["result_fingerprint"], self.baseline_report["result_fingerprint"])

        one_worker_config = _write_config(
            self.config,
            self.root / "config-one-worker.json",
            concurrency=1,
            snapshot_interval=1,
        )
        one_worker_output = self.root / "one-worker"
        one_worker = run_pipeline(
            sample_dir=self.samples,
            config_path=one_worker_config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=one_worker_output,
        )
        self.assertEqual(
            _json_roundtrip(one_worker.report["deterministic_results"]),
            _json_roundtrip(self.baseline_report["deterministic_results"]),
        )
        self.assertEqual(one_worker.report["result_fingerprint"], self.baseline_report["result_fingerprint"])

    def test_threshold_change_invalidates_only_similarity(self) -> None:
        output = self._copy_baseline("threshold-incremental")
        changed = _write_config(self.config, self.root / "threshold-050.json", similarity_threshold=0.5)
        outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=changed,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        incremental = outcome.report["incremental"]
        self.assertEqual(incremental["reused_count"], 24)
        self.assertEqual(incremental["recomputed_count"], 6)
        self.assertEqual({item["kind"] for item in incremental["recomputed"]}, {"similarity"})
        self.assertEqual({item["kind"] for item in incremental["invalidated"]}, {"similarity"})
        deterministic = outcome.report["deterministic_results"]
        self.assertEqual(
            _json_roundtrip({"samples": deterministic["samples"]}),
            _json_roundtrip({"samples": self.baseline_report["deterministic_results"]["samples"]}),
        )

    def test_threshold_boundary_and_precision_sensitive(self) -> None:
        output = self._copy_baseline("boundary-precision")
        baseline_pairs = self.baseline_report["deterministic_results"]["pairs"]
        same_b = next(item for item in baseline_pairs if item["id"] == "same-b-01")
        threshold = same_b["similarity_fixed"] / 1_000_000_000
        changed = _write_config(
            self.config,
            self.root / "threshold-boundary.json",
            similarity_threshold=threshold,
        )
        outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=changed,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        deterministic = outcome.report["deterministic_results"]
        self.assertEqual(deterministic["summary"]["boundary_pair_count"], 1)
        self.assertEqual(deterministic["boundary_pairs"][0]["pair_id"], "same-b-01")
        self.assertTrue(deterministic["boundary_pairs"][0]["threshold_tie"])
        self.assertGreaterEqual(deterministic["summary"]["precision_sensitive_count"], 1)
        self.assertTrue(
            any(item["code"] == "E_PRECISION_SENSITIVE" for item in deterministic["failures"])
        )
        reasons = {item["reason"] for item in deterministic["precision_trace"]["events"]}
        self.assertIn("LOW_VS_HIGH_PRECISION_DECISION", reasons)
        self.assertIn("EMBEDDING_LEVEL_VS_FIXED_SIMILARITY_DECISION", reasons)

    def test_bit_width_and_quantization_levels_change_embeddings(self) -> None:
        low_width_config = _write_config(self.config, self.root / "bits-8.json", fractional_bits=8)
        low_width_output = self.root / "bits-8"
        low_width = run_pipeline(
            sample_dir=self.samples,
            config_path=low_width_config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=low_width_output,
        )
        base_embedding = next(
            item["embedding"]
            for item in self.baseline_report["deterministic_results"]["samples"]
            if item["name"] == "speaker_a/a1.wav"
        )
        low_embedding = next(
            item["embedding"]
            for item in low_width.report["deterministic_results"]["samples"]
            if item["name"] == "speaker_a/a1.wav"
        )
        self.assertNotEqual(base_embedding["vector"], low_embedding["vector"])
        self.assertNotEqual(
            (base_embedding["projection_integer_min"], base_embedding["projection_integer_max"]),
            (low_embedding["projection_integer_min"], low_embedding["projection_integer_max"]),
        )

        output = self._copy_baseline("quant-levels")
        quant_config = _write_config(
            self.config,
            self.root / "quant-64.json",
            quantization_levels=64,
        )
        quant_outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=quant_config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        incremental = quant_outcome.report["incremental"]
        self.assertEqual({item["kind"] for item in incremental["recomputed"]}, {"embedding", "similarity"})
        quant_embedding = next(
            item["embedding"]
            for item in quant_outcome.report["deterministic_results"]["samples"]
            if item["name"] == "speaker_a/a1.wav"
        )
        self.assertNotEqual(base_embedding["level_codes"], quant_embedding["level_codes"])
        self.assertNotEqual(base_embedding["level_code_min"], quant_embedding["level_code_min"])
        self.assertNotEqual(base_embedding["vector"], quant_embedding["vector"])

    def test_mel_change_invalidates_all_stage_artifacts(self) -> None:
        output = self._copy_baseline("mel-incremental")
        changed = _write_config(self.config, self.root / "mel-27.json", mel_bands=27)
        outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=changed,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        incremental = outcome.report["incremental"]
        self.assertEqual(incremental["recomputed_count"], 30)
        self.assertEqual(incremental["reused_count"], 0)
        self.assertEqual(incremental["invalidated_count"], 30)
        self.assertEqual(
            {item["kind"] for item in incremental["recomputed"]},
            {
                "features",
                "feature_failure",
                "aggregation",
                "aggregation_failure",
                "embedding",
                "similarity",
            },
        )

    def test_replacing_one_audio_invalidates_only_its_downstream(self) -> None:
        replacement_samples = self.root / "replacement-samples"
        _copy_tree(self.samples, replacement_samples)
        replacement_output = self.root / "replacement-output"
        first = run_pipeline(
            sample_dir=replacement_samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=replacement_output,
        )
        replacement_wave = synthesise_voice(
            {
                "f0_mean": 101.0,
                "f0_std": 4.0,
                "formants": ((760.0, 90.0), (1400.0, 140.0), (2700.0, 220.0)),
            },
            seed=99173,
            duration_seconds=1.37,
            variant=9,
        )
        write_pcm16_wav(replacement_samples / "speaker_a" / "a2.wav", replacement_wave)
        second = run_pipeline(
            sample_dir=replacement_samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=replacement_output,
        )
        incremental = second.report["incremental"]
        self.assertEqual(incremental["recomputed_count"], 5)
        self.assertEqual(incremental["reused_count"], 25)
        recomputed_labels = {item["label"] for item in incremental["recomputed"]}
        self.assertIn("feature:speaker_a/a2.wav", recomputed_labels)
        self.assertIn("aggregation:speaker_a/a2.wav", recomputed_labels)
        self.assertIn("embedding:speaker_a/a2.wav", recomputed_labels)
        self.assertIn("similarity:same-a-01", recomputed_labels)
        self.assertIn("similarity:cross-ac-01", recomputed_labels)
        self.assertNotIn("similarity:cross-bc-01", recomputed_labels)
        self.assertNotEqual(first.report["result_fingerprint"], second.report["result_fingerprint"])

    def test_recovery_from_fully_replaced_cache_without_state(self) -> None:
        output = self._copy_baseline("recovery-without-state")
        (output / "state" / "state.json").unlink()
        outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.report["result_fingerprint"], self.baseline_report["result_fingerprint"])
        self.assertEqual(outcome.report["incremental"]["reused_count"], 30)
        self.assertEqual(outcome.report["incremental"]["recomputed_count"], 0)

    def test_handled_stop_and_incomplete_config_mismatch(self) -> None:
        output = self.root / "stop-output"
        stop = threading.Event()
        stop.set()
        outcome = run_pipeline(
            sample_dir=self.samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
            stop_event=stop,
        )
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.status, "interrupted")
        self.assertFalse((output / "report.json").exists())
        state = json.loads((output / "state" / "state.json").read_text(encoding="utf-8"))
        self.assertFalse(state["complete"])
        self.assertTrue(state["interrupted"])

        changed = _write_config(self.config, self.root / "incomplete-mismatch.json", mel_bands=27)
        with self.assertRaises(ConfigMismatchError):
            run_pipeline(
                sample_dir=self.samples,
                config_path=changed,
                pairs_path=self.pairs,
                anomalies_path=self.anomalies,
                output_dir=output,
            )
        with self.assertRaises(ConfigMismatchError):
            run_pipeline(
                sample_dir=self.samples,
                config_path=self.config,
                pairs_path=self.pairs,
                anomalies_path=self.anomalies,
                output_dir=self.root / "resume-without-state",
                resume=True,
            )

    def test_incomplete_same_config_resumes(self) -> None:
        output = self.root / "resume-output"
        stop = threading.Event()
        stop.set()
        first = run_pipeline(
            sample_dir=self.samples,
            config_path=self.config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
            stop_event=stop,
        )
        self.assertEqual(first.status, "interrupted")
        resume_config = _write_config(
            self.config,
            self.root / "resume-concurrency-one.json",
            concurrency=1,
            snapshot_interval=1,
        )
        second = run_pipeline(
            sample_dir=self.samples,
            config_path=resume_config,
            pairs_path=self.pairs,
            anomalies_path=self.anomalies,
            output_dir=output,
        )
        self.assertEqual(second.status, "complete")
        self.assertEqual(second.report["incremental"]["snapshot_mode"], "resume")
        self.assertEqual(second.report["result_fingerprint"], self.baseline_report["result_fingerprint"])


if __name__ == "__main__":
    unittest.main()
