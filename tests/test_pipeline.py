from __future__ import annotations

import json
import hashlib
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from face_anim.cli import main
from face_anim.generator import generate_samples
from face_anim.pipeline import run_pipeline


class PipelineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp_root.name)
        cls.samples = cls.root / "samples"
        cls.manifest = generate_samples(cls.samples, seed=20261128)
        cls.output = cls.root / "output"
        cls.summary = run_pipeline(
            list(cls.manifest["version_paths"].values()),
            fps=30,
            threshold=0.05,
            output_dir=cls.output,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_root.cleanup()

    def _copy_samples(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(self.samples, target)
        return target

    def _read(self, path: Path):
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, path: Path, payload) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _tree_hashes(self, root: Path):
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_default_pipeline_passes_and_exports_consistent_frames(self) -> None:
        self.assertEqual(self.summary["status"], "pass")
        curves = self._read(self.output / "channel_curves.json")
        keyframes = self._read(self.output / "face_animation.json")
        self.assertEqual(curves["frame_count"], keyframes["frame_count"])
        self.assertEqual(len(curves["frames"]), curves["frame_count"])
        self.assertEqual(len(keyframes["keyframes"]), keyframes["frame_count"])
        for internal, player in zip(curves["frames"], keyframes["keyframes"]):
            self.assertEqual(internal["frame"], player["frame"])
            self.assertEqual(internal["time"], player["time"])
            self.assertEqual(internal["values"], player["values"])

    def test_exported_lip_curves_do_not_have_single_frame_jumps(self) -> None:
        curves = self._read(self.output / "channel_curves.json")
        for channel_index, channel in enumerate(curves["channels"]):
            values = [
                frame["values"][channel_index] for frame in curves["frames"]
            ]
            max_step = max(
                abs(right - left) for left, right in zip(values, values[1:])
            )
            self.assertLess(max_step, 0.2, channel)

    def test_generator_is_deterministic_for_a_fixed_seed(self) -> None:
        second = self.root / "samples-second"
        generate_samples(second, seed=20261128)
        self.assertEqual(
            self._tree_hashes(self.samples), self._tree_hashes(second)
        )

    def test_pipeline_artifacts_are_reproducible(self) -> None:
        second_output = self.root / "output-second"
        run_pipeline(
            list(self.manifest["version_paths"].values()),
            fps=30,
            threshold=0.05,
            output_dir=second_output,
        )
        self.assertEqual(
            self._tree_hashes(self.output), self._tree_hashes(second_output)
        )

    def test_cross_version_outputs_are_identical(self) -> None:
        diff = self._read(self.output / "cross_version_diff.json")
        self.assertTrue(diff["identical"])
        self.assertEqual(diff["max_abs_deviation"], 0.0)
        self.assertEqual(diff["curve_differences"], [])
        self.assertEqual(diff["keyframe_differences"], [])
        hashes = {
            row["curve_sha256"] for row in diff["versions"]
        }
        self.assertEqual(len(hashes), 1)
        self.assertEqual(
            len({row["keyframe_sha256"] for row in diff["versions"]}), 1
        )

    def test_closed_phonemes_have_zero_jaw_mapping_in_every_version(self) -> None:
        mapping = self._read(self.output / "phoneme_mapping.json")
        self.assertTrue(mapping["closed_visemes_zero_jaw"])
        self.assertTrue(mapping["all_phoneme_targets_consistent"])
        closed = [
            row for row in mapping["phonemes"] if row["canonical"] in {"B", "P", "M"}
        ]
        self.assertEqual({row["viseme"] for row in closed}, {"PP"})
        for row in closed:
            self.assertEqual(row["targets"]["jaw_open"], 0.0)
            self.assertEqual(set(row["labels_by_version"]), {"A", "B", "C"})

    def test_long_closures_reach_a_visible_closed_jaw_plateau(self) -> None:
        truth = self._read(self.samples / "ground_truth.json")
        curves = self._read(self.output / "channel_curves.json")
        jaw_index = curves["channels"].index("jaw_open")
        fps = curves["fps"]
        long_closures = [
            phoneme
            for sentence in truth["sentences"]
            for phoneme in sentence["phonemes"]
            if phoneme["canonical"] in {"B", "P", "M"}
            and phoneme["end_ms"] - phoneme["start_ms"] >= 200
        ]
        self.assertTrue(long_closures)
        for phoneme in long_closures:
            start_frame = round(phoneme["start_ms"] * fps / 1000)
            end_frame = round(phoneme["end_ms"] * fps / 1000)
            values = [
                curves["frames"][frame]["values"][jaw_index]
                for frame in range(start_frame, min(end_frame, curves["frame_count"]))
            ]
            self.assertLess(min(values), 0.08, phoneme["id"])

    def test_emotion_stress_conflict_is_recorded(self) -> None:
        conflicts = self._read(self.output / "emotion_conflicts.json")
        self.assertGreaterEqual(conflicts["conflict_count"], 1)
        first = conflicts["conflicts"][0]
        self.assertEqual(first["code"], "EMOTION_STRESS_CONFLICT")
        self.assertEqual(first["policy"], "stress_over_emotion")
        self.assertEqual(first["selected"], "stress")

    def test_threshold_reports_first_sentence_and_phoneme_evidence(self) -> None:
        output = self.root / "low-threshold"
        summary = run_pipeline(
            list(self.manifest["version_paths"].values()),
            fps=30,
            threshold=0.0,
            output_dir=output,
        )
        self.assertFalse(summary["sync_passed"])
        report = self._read(output / "sync_report.json")
        first = report["first_exceeded"]
        self.assertIsNotNone(first)
        self.assertEqual(first["sentence_id"], "S01")
        self.assertIn("phoneme_id", first["phoneme_basis"])
        self.assertTrue(first["phoneme_basis"]["source_labels"])

    def test_time_overlap_returns_located_error_and_no_animation(self) -> None:
        samples = self._copy_samples("overlap-samples")
        annotation_path = samples / "version_b" / "annotation.json"
        annotation = self._read(annotation_path)
        annotation["sentences"][0]["phonemes"][1]["start_frame"] = 2
        self._write(annotation_path, annotation)
        output = self.root / "overlap-output"
        with redirect_stderr(io.StringIO()):
            code = main(
                [
                    "pipeline",
                    "--samples-dir",
                    str(samples),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(code, 2)
        error = self._read(output / "error.json")
        self.assertEqual(error["error"]["code"], "E_TIME_OVERLAP")
        self.assertFalse((output / "face_animation.json").exists())

    def test_sample_rate_mismatch_returns_located_error(self) -> None:
        samples = self._copy_samples("rate-samples")
        annotation_path = samples / "version_c" / "annotation.json"
        annotation = self._read(annotation_path)
        annotation["project"]["audio_sample_rate"] = 44100
        self._write(annotation_path, annotation)
        output = self.root / "rate-output"
        with redirect_stderr(io.StringIO()):
            code = main(
                [
                    "pipeline",
                    "--samples-dir",
                    str(samples),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(code, 2)
        error = self._read(output / "error.json")
        self.assertEqual(error["error"]["code"], "E_SAMPLE_RATE_MISMATCH")
        self.assertFalse((output / "face_animation.json").exists())

    def test_missing_phoneme_returns_located_error(self) -> None:
        samples = self._copy_samples("missing-samples")
        annotation_path = samples / "version_a" / "annotation.json"
        annotation = self._read(annotation_path)
        del annotation["sentences"][0]["phonemes"][2]["id"]
        self._write(annotation_path, annotation)
        output = self.root / "missing-output"
        with redirect_stderr(io.StringIO()):
            code = main(
                [
                    "pipeline",
                    "--samples-dir",
                    str(samples),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(code, 2)
        error = self._read(output / "error.json")
        self.assertEqual(error["error"]["code"], "E_MISSING_PHONEME")
        self.assertEqual(error["error"]["location"]["sentence_id"], "S01")
        self.assertFalse((output / "face_animation.json").exists())


if __name__ == "__main__":
    unittest.main()
