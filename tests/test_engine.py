from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emotion_engine.aggregation import (  # noqa: E402
    assert_stream_batch_match,
    first_frame_deviation,
)
from emotion_engine.batch import OfflineEngine, load_jsonl, load_wav  # noqa: E402
from emotion_engine.cache import ArtifactCache  # noqa: E402
from emotion_engine.config import config_from_dict, load_config  # noqa: E402
from emotion_engine.errors import (  # noqa: E402
    E_CHUNK_GAP,
    E_CHUNK_ORDER,
    E_CHUNK_RANGE,
    E_CONFIG_INVALID,
    E_SEGMENT_BOUNDARY,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_SPEAKER_LABEL_INVALID,
    E_STREAM_BATCH_MISMATCH,
    E_WINDOW_RECOUNT,
    EngineError,
)
from emotion_engine.features import pcm_content_hash  # noqa: E402
from emotion_engine.segments import validate_segment_boundaries  # noqa: E402
from emotion_engine.snapshot import Snapshot  # noqa: E402
from emotion_engine.streaming import StreamingEngine  # noqa: E402
from emotion_engine.synth import generate_samples  # noqa: E402
from emotion_engine.tiering import classify_sequence  # noqa: E402
from emotion_engine.util import intervals_cover_all, intervals_overlap  # noqa: E402
from emotion_engine.windows import recount_window  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_entry(root: Path, index: int = 0):
    manifest_path = root / "dialogs.jsonl"
    manifest = load_jsonl(manifest_path)
    entry = manifest[index]
    wav_path = manifest_path.parent / entry["wav_path"]
    truth_path = manifest_path.parent / entry["truth_path"]
    pcm, sample_rate = load_wav(wav_path)
    truth = load_jsonl(truth_path)
    return manifest, entry, pcm, sample_rate, truth, wav_path, truth_path, manifest_path


def _run_stream(entry, pcm, sample_rate, truth, config, clock_ns=None):
    engine = StreamingEngine(entry, config, clock_ns=clock_ns or __import__("time").perf_counter_ns)
    chunk_samples = round(int(entry["chunk_ms"]) * sample_rate / 1000)
    hop_samples = config.hop_samples(sample_rate)
    for sequence, start in enumerate(range(0, int(pcm.shape[0]), chunk_samples)):
        end = min(int(pcm.shape[0]), start + chunk_samples)
        labels = [
            str(truth[index]["speaker_id"])
            for index in range(start // hop_samples, min(end // hop_samples, len(truth)))
        ]
        engine.submit_chunk(
            sequence,
            round(start * 1000 / sample_rate),
            pcm[start:end],
            labels,
            checkpoint=True,
        )
    return engine, engine.finish()


class EngineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.sample_root = Path(cls.temp.name) / "samples"
        cls.config = load_config(ROOT / "config" / "default.json")
        generate_samples(cls.sample_root, cls.config)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_sample_contract_and_deterministic_generation(self) -> None:
        (
            manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            wav_path,
            truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        self.assertEqual(len(manifest), 2)
        self.assertEqual(sample_rate, 16000)
        self.assertEqual(pcm.dtype, np.dtype("<i2"))
        self.assertEqual(entry["duration_ms"], 1800)
        self.assertEqual(entry["chunk_ms"], 80)
        self.assertEqual(len(truth), 90)
        for index, label in enumerate(truth):
            self.assertEqual(
                set(label),
                {"frame_index", "speaker_id", "valence", "arousal", "is_speech"},
            )
            self.assertEqual(label["frame_index"], index)
            self.assertIn(label["speaker_id"], entry["speakers"])

        with tempfile.TemporaryDirectory() as second:
            generate_samples(second, self.config)
            second_root = Path(second)
            self.assertEqual(_sha(wav_path), _sha(second_root / entry["wav_path"]))
            self.assertEqual(_sha(truth_path), _sha(second_root / entry["truth_path"]))
            self.assertEqual(
                _sha(self.sample_root / "dialogs.jsonl"),
                _sha(second_root / "dialogs.jsonl"),
            )

    def test_offline_stream_exact_for_both_dialogues(self) -> None:
        for dialog_index in (0, 1):
            with self.subTest(dialog_index=dialog_index):
                (
                    _manifest,
                    entry,
                    pcm,
                    sample_rate,
                    truth,
                    _wav,
                    _truth_path,
                    _manifest_path,
                ) = _load_entry(self.sample_root, dialog_index)
                speakers = [item["speaker_id"] for item in truth]
                allowed = [str(item) for item in entry["speakers"]]
                offline = OfflineEngine(self.config).analyze_pcm(
                    pcm,
                    sample_rate,
                    speakers,
                    allowed,
                    truth=truth,
                    verify_recounts=True,
                )
                engine, stream = _run_stream(entry, pcm, sample_rate, truth, self.config)
                self.assertIsNone(first_frame_deviation(stream, offline, self.config))
                self.assertEqual(stream["checksums"]["frames"], offline["checksums"]["frames"])
                self.assertEqual(stream["checksums"]["segments"], offline["checksums"]["segments"])
                self.assertEqual(stream["summary"], offline["summary"])
                self.assertEqual(stream["segments"], offline["segments"])
                self.assertTrue(stream["self_check"]["ok"])
                self.assertEqual(len(engine.latency_observations), (entry["duration_ms"] + entry["chunk_ms"] - 1) // entry["chunk_ms"])
                for frame in offline["frames"]:
                    self.assertEqual(
                        frame["start_sample"],
                        frame["frame_index"] * self.config.hop_samples(sample_rate),
                    )
                    self.assertEqual(
                        frame["end_sample"],
                        frame["start_sample"] + self.config.frame_samples(sample_rate),
                    )

    def test_window_recount_and_boundary_semantics(self) -> None:
        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        view = OfflineEngine(self.config).analyze_pcm(
            pcm,
            sample_rate,
            [item["speaker_id"] for item in truth],
            [str(item) for item in entry["speakers"]],
            verify_recounts=False,
        )
        for index, frame in enumerate(view["frames"]):
            recounted = recount_window(index, view["frames"], self.config)
            self.assertEqual(recounted, frame["window"])
        self.assertEqual(view["frames"][0]["window"]["window_size"], 1)
        self.assertEqual(view["frames"][4]["window"]["window_size"], 5)
        self.assertEqual(view["frames"][4]["window"]["window_start_frame"], 0)
        self.assertTrue(view["frames"][0]["window"]["insufficient_window"])
        self.assertFalse(view["frames"][4]["window"]["insufficient_window"])
        tampered = copy.deepcopy(view["frames"])
        tampered[10]["window"]["tier"] = "definitely-wrong"
        with self.assertRaises(EngineError) as context:
            recount_window(10, tampered, self.config)
        self.assertEqual(context.exception.code, E_WINDOW_RECOUNT)
        with self.assertRaises(EngineError) as context:
            recount_window(len(view["frames"]), view["frames"], self.config)
        self.assertEqual(context.exception.code, E_WINDOW_RECOUNT)

    def test_content_cache_reuse_and_selective_invalidation(self) -> None:
        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        speakers = [item["speaker_id"] for item in truth]
        allowed = [str(item) for item in entry["speakers"]]
        cache = ArtifactCache()
        engine = OfflineEngine(self.config, cache)
        first = engine.analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed,
            verify_recounts=False,
        )
        second = engine.analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed,
            verify_recounts=False,
        )
        self.assertEqual(first["checksums"]["frames"], second["checksums"]["frames"])
        self.assertEqual(first["snapshot_version"], 1)
        self.assertEqual(second["snapshot_version"], 2)
        self.assertEqual(
            {kind: item["version"] for kind, item in first["snapshots"].items()},
            {"frames": 1, "segments": 1, "windows": 1},
        )
        self.assertEqual(second["cache"]["mode"], "full_reuse")
        self.assertEqual(second["cache"]["recomputed_frame_intervals"], [])
        self.assertEqual(second["cache"]["reused_frame_intervals"], [[0, len(speakers) - 1]])

        changed_raw = copy.deepcopy(self.config.raw)
        changed_raw["window"]["length_frames"] = 4
        changed = config_from_dict(changed_raw)
        third = OfflineEngine(changed, cache).analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed,
            verify_recounts=False,
        )
        report = third["cache"]
        self.assertEqual(report["mode"], "selective_rebuild")
        self.assertTrue(third["feature_cache"]["feature_cache_hit"])
        self.assertTrue(intervals_cover_all(
            report["recomputed_frame_intervals"],
            report["reused_frame_intervals"],
            third["frame_count"],
        ))
        self.assertFalse(intervals_overlap(
            report["recomputed_frame_intervals"],
            report["reused_frame_intervals"],
        ))
        self.assertEqual(report["recomputed_frame_intervals"], [[3, len(speakers) - 1]])
        self.assertEqual(report["reused_frame_intervals"], [[0, 2]])
        self.assertTrue(report["recomputed_segment_ids"])
        for index in range(third["frame_count"]):
            recount_window(index, third["frames"], changed)

        changed_raw["threshold"]["strategy"] = "strict"
        strict_config = config_from_dict(changed_raw)
        fourth = OfflineEngine(strict_config, cache).analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed,
            verify_recounts=False,
        )
        strict_report = fourth["cache"]
        self.assertEqual(strict_report["mode"], "selective_rebuild")
        self.assertTrue(strict_report["recomputed_frame_intervals"])
        self.assertTrue(strict_report["reused_frame_intervals"])
        self.assertTrue(intervals_cover_all(
            strict_report["recomputed_frame_intervals"],
            strict_report["reused_frame_intervals"],
            fourth["frame_count"],
        ))
        self.assertNotEqual(
            pcm_content_hash(pcm, sample_rate),
            pcm_content_hash(np.concatenate([pcm[:-1], np.array([pcm[-1] ^ 1], dtype=np.int16)]), sample_rate),
        )

    def test_threshold_hysteresis_is_reproducible(self) -> None:
        values = [0.140, 0.149, 0.151, 0.200, 0.190, 0.140, 0.155, 0.149]
        frames = [
            {"frame_index": index, "valence": value, "arousal": 0.01}
            for index, value in enumerate(values)
        ]
        first = classify_sequence(frames, self.config)
        second = classify_sequence(frames, self.config)
        self.assertEqual(first, second)
        self.assertGreater(first["threshold_stay_count"], 0)
        self.assertTrue(first["near_threshold_frames"])
        raw = copy.deepcopy(self.config.raw)
        raw["threshold"]["strategy"] = "strict"
        strict = classify_sequence(frames, config_from_dict(raw))
        self.assertNotEqual(first["tiers"], strict["tiers"])

    def test_config_speaker_and_segment_business_errors(self) -> None:
        raw = copy.deepcopy(self.config.raw)
        raw["threshold"]["strategy"] = "invalid"
        with self.assertRaises(EngineError) as context:
            config_from_dict(raw)
        self.assertEqual(context.exception.code, E_CONFIG_INVALID)
        self.assertEqual(context.exception.details["field"], "threshold.strategy")

        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        speakers = [item["speaker_id"] for item in truth]
        allowed = [str(item) for item in entry["speakers"]]
        bad = list(speakers)
        bad[0] = "spk_unknown"
        with self.assertRaises(EngineError) as context:
            OfflineEngine(self.config).analyze_pcm(
                pcm,
                sample_rate,
                bad,
                allowed,
                verify_recounts=False,
            )
        self.assertEqual(context.exception.code, E_SPEAKER_LABEL_INVALID)
        self.assertEqual(context.exception.details["frame_index"], 0)

        view = OfflineEngine(self.config).analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            allowed,
            verify_recounts=False,
        )
        malformed = copy.deepcopy(view["segments"])
        malformed[0]["end_frame"] = malformed[1]["start_frame"]
        with self.assertRaises(EngineError) as context:
            validate_segment_boundaries(malformed, speakers)
        self.assertEqual(context.exception.code, E_SEGMENT_BOUNDARY)
        self.assertIn("first_deviating_frame", context.exception.details)

    def test_chunk_order_gap_and_range_errors(self) -> None:
        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        chunk_samples = round(int(entry["chunk_ms"]) * sample_rate / 1000)
        labels = [
            truth[index]["speaker_id"]
            for index in range(chunk_samples // self.config.hop_samples(sample_rate))
        ]

        with self.assertRaises(EngineError) as context:
            StreamingEngine(entry, self.config).submit_chunk(0, 80, pcm[:chunk_samples], labels)
        self.assertEqual(context.exception.code, E_CHUNK_ORDER)

        with self.assertRaises(EngineError) as context:
            StreamingEngine(entry, self.config).submit_chunk(1, 0, pcm[:chunk_samples], labels)
        self.assertEqual(context.exception.code, E_CHUNK_GAP)

        engine = StreamingEngine(entry, self.config)
        engine.submit_chunk(0, 0, pcm[:chunk_samples], labels)
        with self.assertRaises(EngineError) as context:
            engine.submit_chunk(0, 80, pcm[chunk_samples : 2 * chunk_samples], labels)
        self.assertEqual(context.exception.code, E_CHUNK_GAP)

        oversized = np.zeros(int(pcm.shape[0]) + self.config.hop_samples(sample_rate), dtype=np.int16)
        with self.assertRaises(EngineError) as context:
            StreamingEngine(entry, self.config).submit_chunk(0, 0, oversized, [])
        self.assertEqual(context.exception.code, E_CHUNK_RANGE)

    def test_snapshots_and_stream_batch_mismatch_guard(self) -> None:
        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        engine, stream = _run_stream(entry, pcm, sample_rate, truth, self.config)
        metadata = stream["snapshots"]
        self.assertEqual(
            {kind: value["version"] for kind, value in metadata.items()},
            {"frames": stream["snapshot_version"], "segments": stream["snapshot_version"], "windows": stream["snapshot_version"]},
        )
        with self.assertRaises(EngineError) as context:
            engine.snapshots.replace(Snapshot.create("frames", 0, stream["frames"]))
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)
        with self.assertRaises(EngineError) as context:
            engine.snapshots.replace(
                Snapshot(
                    kind="frames",
                    version=stream["snapshot_version"] + 1,
                    payload=[],
                    checksum="bad",
                )
            )
        self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)

        tampered = copy.deepcopy(stream)
        tampered["frames"][3]["tier"] = "unexpected"
        with self.assertRaises(EngineError) as context:
            assert_stream_batch_match(tampered, stream, self.config)
        self.assertEqual(context.exception.code, E_STREAM_BATCH_MISMATCH)
        self.assertEqual(context.exception.details["first_deviation"]["frame_index"], 3)
        self.assertIn("stream_stats", context.exception.details)
        self.assertIn("batch_stats", context.exception.details)

    def test_latency_uses_observed_clock_and_estimate_is_not_timeout(self) -> None:
        (
            _manifest,
            entry,
            pcm,
            sample_rate,
            truth,
            _wav,
            _truth_path,
            _manifest_path,
        ) = _load_entry(self.sample_root)
        ticks = iter(range(0, 10_000_000_000, 2_000_000))
        engine = StreamingEngine(entry, self.config, clock_ns=lambda: next(ticks))
        chunk_samples = round(int(entry["chunk_ms"]) * sample_rate / 1000)
        labels = [
            truth[index]["speaker_id"]
            for index in range(chunk_samples // self.config.hop_samples(sample_rate))
        ]
        result = engine.submit_chunk(0, 0, pcm[:chunk_samples], labels, checkpoint=True)
        latency = result["latency"]
        self.assertEqual(latency["observed_processing_ns"], 2_000_000)
        self.assertEqual(latency["observed_processing_ms"], 2.0)
        self.assertTrue(latency["estimate"]["estimate_only"])
        self.assertFalse(latency["estimate"]["used_for_timeout"])
        self.assertEqual(latency["estimate"]["timeout_decision"], "not_applicable")
        self.assertEqual(latency["processing_clock"], "time.perf_counter_ns")

        pending = StreamingEngine(entry, self.config)
        with self.assertRaises(EngineError) as context:
            pending.recount_frame(0)
        self.assertEqual(context.exception.code, E_WINDOW_RECOUNT)


if __name__ == "__main__":
    unittest.main()
