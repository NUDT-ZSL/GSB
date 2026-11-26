from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from emotion_diag.anomalies import detect_turn_anomalies
from emotion_diag.cache import ContentCache, plan_truth_invalidation
from emotion_diag.canonical import atomic_write_json, read_json, sha256_hex
from emotion_diag.config import EngineConfig
from emotion_diag.emotions import resolve_tie
from emotion_diag.errors import DiagnosticError, ErrorCode
from emotion_diag.features import extract_features
from emotion_diag.incremental import run_generation_update, run_truth_correction
from emotion_diag.ledger import read_ledger, write_v2_ledger
from emotion_diag.migrate import (
    downgrade_v2_to_v1,
    roundtrip_check,
    upgrade_v1_to_v2,
)
from emotion_diag.pipeline import run_pipeline
from emotion_diag.samplegen import generate_sample_bundle
from emotion_diag.scheduler import check_admission, run_state_machine
from emotion_diag.snapshots import ForcedTermination


TEMP_ROOT = Path(tempfile.mkdtemp(prefix="emotion_diag_test_"))
SAMPLE_DIR = TEMP_ROOT / "samples"
ARTIFACT_DIR = TEMP_ROOT / "artifacts"
MANIFEST = generate_sample_bundle(SAMPLE_DIR)
REPORT = run_pipeline(SAMPLE_DIR, ARTIFACT_DIR, use_process_pool=False)


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


class SampleFeatureTests(unittest.TestCase):
    def test_exact_frame_boundaries_and_deterministic_features(self):
        from emotion_diag.pipeline import load_sample_bundle

        bundle = load_sample_bundle(SAMPLE_DIR)
        config = bundle["config"]
        truth = bundle["truth"]
        features = extract_features(
            bundle["pcm"], config, expected_frame_count=len(truth)
        )
        self.assertEqual(len(features), MANIFEST["frame_count"])
        for index, row in enumerate(truth):
            self.assertEqual(row["start_sample"], index * config.hop_samples)
            self.assertEqual(
                row["end_sample"], row["start_sample"] + config.frame_samples
            )
            self.assertEqual(features[index]["start_sample"], row["start_sample"])
            self.assertEqual(features[index]["end_sample"], row["end_sample"])

        again = extract_features(bundle["pcm"], config)
        self.assertEqual(
            json.dumps(features, sort_keys=True),
            json.dumps(again, sort_keys=True),
        )
        self.assertTrue(any(f["f0_hz"] > 0 for f in features))

    def test_generator_is_byte_deterministic(self):
        second = TEMP_ROOT / "samples_again"
        generate_sample_bundle(second)
        for name in [
            "dialogue_16k_mono.pcm",
            "frame_truth.jsonl",
            "emotion_annotations_v1.json",
            "manifest.json",
        ]:
            left = (SAMPLE_DIR / name).read_bytes()
            right = (second / name).read_bytes()
            self.assertEqual(left, right, name)


class StateMachineTests(unittest.TestCase):
    def test_state_records_and_tie_break(self):
        states = read_json(ARTIFACT_DIR / "diagnostic_report.json")
        state_rows = []
        with (ARTIFACT_DIR / "emotion_states.jsonl").open(encoding="utf-8") as h:
            for line in h:
                state_rows.append(json.loads(line))
        self.assertEqual(len(state_rows), REPORT["frame_count"])
        truth = []
        with (SAMPLE_DIR / "frame_truth.jsonl").open(encoding="utf-8") as h:
            for line in h:
                truth.append(json.loads(line))
        for row, truth_row in zip(state_rows, truth):
            self.assertEqual(row["speaker_id"], truth_row["speaker_id"])
            self.assertIn("energy_change_rate", row["basis"])
            self.assertIn("zcr_gradient", row["basis"])

        candidates = [
            {"state": "happy", "score": 1.0, "speaker_id": 3, "frame_index": 9},
            {"state": "angry", "score": 1.0, "speaker_id": 1, "frame_index": 20},
            {"state": "calm", "score": 1.0, "speaker_id": 1, "frame_index": 5},
        ]
        winner = resolve_tie(candidates)
        self.assertEqual((winner["speaker_id"], winner["frame_index"]), (1, 5))

    def test_process_pool_merge_is_ordered(self):
        from emotion_diag.pipeline import load_sample_bundle

        bundle = load_sample_bundle(SAMPLE_DIR)
        config = bundle["config"]
        config.values["workers"] = 2
        config.values["batch_frames"] = 16
        n = 96
        result = run_state_machine(
            extract_features(bundle["pcm"][: n * config.hop_samples + config.frame_samples], config)[:n],
            bundle["truth"][:n],
            config,
            use_process_pool=True,
        )
        self.assertTrue(result["used_process_pool"])
        self.assertEqual(result["dispatch_order"], result["merge_order"])
        self.assertEqual(
            [r["frame_index"] for r in result["records"]], list(range(n))
        )


class AnomalyTests(unittest.TestCase):
    def test_injected_events_detected(self):
        anomalies = read_json(ARTIFACT_DIR / "anomalies.json")
        self.assertEqual(anomalies["anomaly_count"], 3)
        by_type = {e["type"]: e for e in anomalies["events"]}
        self.assertEqual(
            (by_type["abnormal_pause"]["start_frame"], by_type["abnormal_pause"]["end_frame"]),
            (600, 690),
        )
        self.assertEqual(
            (by_type["floor_grab"]["start_frame"], by_type["floor_grab"]["end_frame"]),
            (910, 940),
        )
        self.assertEqual(
            (by_type["interruption"]["start_frame"], by_type["interruption"]["end_frame"]),
            (1160, 1190),
        )
        for event in anomalies["events"]:
            self.assertGreater(event["confidence"], 0.8)
            self.assertIn("summary_text", event["evidence"])

    def test_clean_sequence_returns_empty_marker(self):
        config = EngineConfig.load()
        truth = [
            {"frame_index": i, "speaker_id": 1, "is_speech": True, "turn_label": "t"}
            for i in range(20)
        ]
        features = [{"rms": 0.1, "zcr": 0.1} for _ in range(20)]
        result = detect_turn_anomalies(truth, features, config)
        self.assertTrue(result["empty"])
        self.assertEqual(result["marker"], "ANOMALY_LIST_EMPTY")


class LedgerErrorTests(unittest.TestCase):
    def test_version_and_v1_errors(self):
        root = TEMP_ROOT / "ledger_errors"
        unknown = root / "unknown.json"
        write_json(unknown, {"format": "emotion_ledger_v9", "version": "9.0"})
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(unknown)
        self.assertEqual(ctx.exception.code, ErrorCode.VERSION_UNSUPPORTED)

        missing = root / "missing.json"
        write_json(
            missing,
            {"format": "emotion_ledger_v1", "version": "1.0", "records": [{"time_s": 1.0}]},
        )
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(missing)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)

        parse_error = root / "parse.json"
        write_json(
            parse_error,
            {
                "format": "emotion_ledger_v1",
                "version": "1.0",
                "records": [{"time_s": "not-number", "emotion_tier": "happy"}],
            },
        )
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(parse_error)
        self.assertEqual(ctx.exception.code, ErrorCode.PARSE_ERROR)

    def test_v2_corruption_mapping(self):
        root = TEMP_ROOT / "v2_corrupt"
        truth = [
            {"frame_index": i, "speaker_id": i % 2 + 1}
            for i in range(4)
        ]
        config = EngineConfig.load()
        good = root / "good"
        write_v2_ledger(good, truth, ["neutral"] * 4, config)
        ledger = read_ledger(good)
        self.assertEqual(len(ledger["records"]), 4)

        regression = root / "regression"
        shutil.copytree(good, regression)
        shard_path = regression / "shards" / "shard-000.json"
        payload = read_json(shard_path)
        payload["records"][2]["abs_ms"] = 5
        payload["records"][2]["abs_start_ms"] = 5
        write_json(shard_path, payload)
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(regression)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)

        checksum = root / "checksum"
        shutil.copytree(good, checksum)
        shard_path = checksum / "shards" / "shard-000.json"
        payload = read_json(shard_path)
        payload["records"][0]["emotion_tier"] = "angry"
        write_json(shard_path, payload)
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(checksum)
        self.assertEqual(ctx.exception.code, ErrorCode.CHECKSUM_MISMATCH)
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(checksum / "shards" / "shard-000.json")
        self.assertEqual(ctx.exception.code, ErrorCode.CHECKSUM_MISMATCH)

        missing = root / "missing_v2"
        shutil.copytree(good, missing)
        shard_path = missing / "shards" / "shard-000.json"
        payload = read_json(shard_path)
        del payload["records"][0]["generation"]
        write_json(shard_path, payload)
        with self.assertRaises(DiagnosticError) as ctx:
            read_ledger(missing)
        self.assertEqual(ctx.exception.code, ErrorCode.FORMAT_CORRUPT)


class MigrationTests(unittest.TestCase):
    def test_cross_version_and_roundtrip(self):
        summary = read_json(ARTIFACT_DIR / "migration_summary.json")
        self.assertTrue(summary["cross_version_equivalent"])
        self.assertTrue(summary["roundtrip_equivalent"])
        self.assertTrue(summary["against_original_equivalent"])
        self.assertGreater(summary["completion_count"], 0)
        v1 = read_ledger(SAMPLE_DIR / "emotion_annotations_v1.json")
        truth = []
        with (SAMPLE_DIR / "frame_truth.jsonl").open(encoding="utf-8") as h:
            for line in h:
                truth.append(json.loads(line))
        upgrade = upgrade_v1_to_v2(
            v1, truth, EngineConfig.load(SAMPLE_DIR / "config.json")
        )
        self.assertTrue(upgrade["equivalence"]["equivalent"])

        bundle_v2 = read_ledger(SAMPLE_DIR / "emotion_annotations_v2")
        truth = []
        with (SAMPLE_DIR / "frame_truth.jsonl").open(encoding="utf-8") as h:
            for line in h:
                truth.append(json.loads(line))
        result = roundtrip_check(bundle_v2, truth, EngineConfig.load(SAMPLE_DIR / "config.json"))
        self.assertTrue(result["against_original"]["equivalent"])

    def test_strict_revision_loss_returns_semantic_loss(self):
        root = TEMP_ROOT / "v2_revision"
        truth = [{"frame_index": 0, "speaker_id": 1}]
        config = EngineConfig.load()
        write_v2_ledger(root, truth, ["neutral"], config, generation=2, revision=7)
        ledger = read_ledger(root)
        result = downgrade_v2_to_v1(ledger, config)
        self.assertEqual(result["discard_count"], 1)
        self.assertEqual(result["discard_records"][0]["field"], "revision")
        with self.assertRaises(DiagnosticError) as ctx:
            downgrade_v2_to_v1(ledger, config, strict=True)
        self.assertEqual(ctx.exception.code, ErrorCode.SEMANTIC_LOSS)


class CacheIncrementalTests(unittest.TestCase):
    def test_cache_key_inputs_change_hash(self):
        key1 = ContentCache.make_cache_key(
            category="x",
            pcm_hash="a",
            frame_params_hash="b",
            annotation_generation=1,
            upstream_hashes=["u1"],
            frame_index=3,
        )
        key2 = ContentCache.make_cache_key(
            category="x",
            pcm_hash="a",
            frame_params_hash="b",
            annotation_generation=2,
            upstream_hashes=["u1"],
            frame_index=3,
        )
        key3 = ContentCache.make_cache_key(
            category="x",
            pcm_hash="a",
            frame_params_hash="b",
            annotation_generation=1,
            upstream_hashes=["u2"],
            frame_index=3,
        )
        self.assertNotEqual(key1, key2)
        self.assertNotEqual(key1, key3)

    def test_truth_and_generation_invalidation(self):
        report = run_truth_correction(SAMPLE_DIR, ARTIFACT_DIR, frame_index=650)
        self.assertEqual(report["state_reused_ranges"], [[0, 650]])
        self.assertEqual(report["state_recompute_ranges"], [[650, MANIFEST["frame_count"]]])
        self.assertEqual(report["recomputed_state_frames"], MANIFEST["frame_count"] - 650)

        generation = run_generation_update(SAMPLE_DIR, ARTIFACT_DIR, revised_start=760)
        self.assertEqual(generation["ledger_reused_ranges"], [[0, 760]])
        self.assertEqual(
            generation["ledger_recompute_ranges"],
            [[760, MANIFEST["frame_count"]]],
        )


class BudgetTests(unittest.TestCase):
    def test_loop_budget_has_priority(self):
        with self.assertRaises(DiagnosticError) as ctx:
            check_admission(
                frame_count=100,
                resident_feature_bytes=10**12,
                max_transitions=10,
                max_feature_memory_bytes=1024,
                workers=1,
                batch_frames=10,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.LOOP_BUDGET)
        self.assertEqual(
            ctx.exception.details["degradation_category"],
            "loop_budget_rejected",
        )

    def test_resource_limit_category(self):
        with self.assertRaises(DiagnosticError) as ctx:
            check_admission(
                frame_count=10,
                resident_feature_bytes=4096,
                max_transitions=100,
                max_feature_memory_bytes=1024,
                workers=1,
                batch_frames=5,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.RESOURCE_LIMIT)
        self.assertEqual(
            ctx.exception.details["degradation_category"],
            "resident_feature_memory_rejected",
        )


class RecoveryTests(unittest.TestCase):
    def test_uncommitted_state_stage_is_discarded_and_rebuilt(self):
        recovery_dir = TEMP_ROOT / "recovery"
        with self.assertRaises(ForcedTermination):
            run_pipeline(
                SAMPLE_DIR,
                recovery_dir,
                use_process_pool=False,
                crash={"stage": "state_anomaly", "after_frame": 300},
            )
        self.assertTrue((recovery_dir / "state" / "work" / "state_anomaly.work.json").exists())
        report = run_pipeline(SAMPLE_DIR, recovery_dir, use_process_pool=False)
        self.assertIn(
            "state/work/state_anomaly.work.json",
            report["recovery"]["discarded_uncommitted"],
        )
        self.assertEqual(
            report["stage_reuse"]["feature_extraction"]["reused_ranges"],
            [[0, MANIFEST["frame_count"]]],
        )
        self.assertEqual(
            report["stage_reuse"]["state_anomaly"]["recomputed_ranges"],
            [[0, MANIFEST["frame_count"]]],
        )
        states_clean = (ARTIFACT_DIR / "emotion_states.jsonl").read_bytes()
        states_recovered = (recovery_dir / "emotion_states.jsonl").read_bytes()
        self.assertEqual(states_clean, states_recovered)
        anomalies_clean = read_json(ARTIFACT_DIR / "anomalies.json")
        anomalies_recovered = read_json(recovery_dir / "anomalies.json")
        self.assertEqual(anomalies_clean, anomalies_recovered)
        versions = [s["version"] for s in report["stages"]]
        self.assertEqual(versions, [1, 2, 3, 4])
        report_snapshot = read_json(recovery_dir / "state" / "report_export.latest.json")
        self.assertEqual(
            report_snapshot["artifact_checksums"]["diagnostic_report.json"],
            sha256_hex((recovery_dir / "diagnostic_report.json").read_bytes()),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
