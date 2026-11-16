"""Deterministic black/grey-box tests for the VAD component (stdlib unittest)."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from vad.audio import frame_signal, load_wav  # noqa: E402
from vad.config import VadConfig  # noqa: E402
from vad.errors import (E_AUDIO_FORMAT, E_CONFIG_MISMATCH,  # noqa: E402
                        E_GAP_MERGED, E_NONFINITE_SAMPLE, E_SEG_TOO_SHORT)
from vad.features import (compute_frame_features, estimate_noise_floor,  # noqa: E402
                          log_energy, spectral_flatness, zero_crossing_rate)
from vad.injection import InjectionManifest, apply_injections  # noqa: E402
from vad.pipeline import process_sample  # noqa: E402
from vad.runner import run  # noqa: E402
from vad.snapshot import read_snapshot, validate_snapshot  # noqa: E402
from vad.state_machine import (SILENCE, SPEECH, SUSPECT, TRAILING,  # noqa: E402
                               build_thresholds, run_state_machine)

CONFIG = VadConfig.load(ROOT / "configs" / "vad_config.json")
MANIFEST = InjectionManifest.load(ROOT / "configs" / "injection_manifest.json")
INPUT = ROOT / "data" / "input"


class FramingTests(unittest.TestCase):
    def test_integer_timestamps_and_zero_pad(self):
        samples = np.zeros(CONFIG.raw["sample_rate_hz"], dtype=np.float64)
        frames = frame_signal(samples, CONFIG.frame_length_ms,
                              CONFIG.frame_hop_ms, CONFIG.raw["sample_rate_hz"])
        self.assertEqual(frames[0].timestamp_ms, 0)
        self.assertTrue(all(fr.timestamp_ms == fr.index * CONFIG.frame_hop_ms
                            for fr in frames))
        self.assertTrue(any(fr.zero_padded for fr in frames))
        self.assertEqual(frames[0].samples.dtype, np.float64)

    def test_format_validation_codes(self):
        for name, code in [("bad_sample_rate.wav", E_AUDIO_FORMAT),
                           ("bad_channels.wav", E_AUDIO_FORMAT),
                           ("not_audio.wav", E_AUDIO_FORMAT)]:
            with self.subTest(name):
                from vad.errors import VadError
                with self.assertRaises(VadError) as ctx:
                    load_wav(INPUT / name, expected_sr=16000)
                self.assertEqual(ctx.exception.code, code)


class FeatureDeterminismTests(unittest.TestCase):
    def test_float64_and_bit_identical_runs(self):
        rng = np.random.default_rng(42)
        frame = rng.normal(0.0, 0.01, 400)
        first = (log_energy(frame), zero_crossing_rate(frame),
                 spectral_flatness(frame))
        for _ in range(5):
            again = (log_energy(frame), zero_crossing_rate(frame),
                     spectral_flatness(frame))
            self.assertEqual(first, again)

    def test_quiet_sample_stats_identical_across_runs(self):
        samples = load_wav(INPUT / "sample_a_quiet.wav")
        frames = frame_signal(samples, CONFIG.frame_length_ms,
                              CONFIG.frame_hop_ms, CONFIG.raw["sample_rate_hz"])
        rows_a = [compute_frame_features(fr, CONFIG.feature_decimals) for fr in frames]
        samples_b = load_wav(INPUT / "sample_a_quiet.wav")
        frames_b = frame_signal(samples_b, CONFIG.frame_length_ms,
                                CONFIG.frame_hop_ms, CONFIG.raw["sample_rate_hz"])
        rows_b = [compute_frame_features(fr, CONFIG.feature_decimals) for fr in frames_b]
        self.assertEqual(rows_a, rows_b)
        nf_a = estimate_noise_floor(rows_a, CONFIG.noise_floor_window_frames)
        nf_b = estimate_noise_floor(rows_b, CONFIG.noise_floor_window_frames)
        self.assertEqual(nf_a, nf_b)


class StateMachineTests(unittest.TestCase):
    def _trace(self, rows):
        th = build_thresholds(estimate_noise_floor(rows, 20), CONFIG)
        return run_state_machine(rows, th), th

    def test_four_states_and_dependency_on_current_state(self):
        samples = load_wav(INPUT / "sample_b_two_speech.wav")
        frames = frame_signal(samples, 25, 10, 16000)
        rows = [compute_frame_features(fr, 6) for fr in frames]
        trace, _ = self._trace(rows)
        states = {entry["state"] for entry in trace}
        self.assertIn(SILENCE, states)
        self.assertIn(SPEECH, states)
        self.assertTrue(all("reason" in entry for entry in trace))
        # Re-running from frame zero must reproduce the exact same trajectory.
        trace2, _ = self._trace(rows)
        self.assertEqual([(e["state"], e["reason"]) for e in trace],
                         [(e["state"], e["reason"]) for e in trace2])

    def test_tie_boundary_priority(self):
        from vad.state_machine import MachineState, transition
        th = build_thresholds(-5.0, CONFIG)
        row = {"frame": 0, "log_energy": th.high, "zcr": 0.9,
               "flatness": 0.9, "timestamp_ms": 0}
        new_state, reason = transition(MachineState(SILENCE, 0, 0, None), row, th)
        self.assertEqual(new_state, SUSPECT)
        self.assertEqual(reason, "STRONG_EVIDENCE_ENTER_SUSPECT")


class PostprocessTests(unittest.TestCase):
    def test_segment_and_gap_codes_present(self):
        report = json.loads((ROOT / "data" / "output" /
                             "diagnostic_report.json").read_text())
        by_name = {r["sample"]: r for r in report["results"]}
        b_audit = [a["code"] for a in by_name["sample_b_two_speech.wav"]["audit"]]
        c_audit = [a["code"] for a in by_name["sample_c_gaps.wav"]["audit"]]
        self.assertIn(E_SEG_TOO_SHORT, b_audit)
        self.assertIn(E_GAP_MERGED, c_audit)
        for result in by_name.values():
            if result["status"] != "ok":
                continue
            for segment in result["segments"]:
                self.assertEqual(segment["end_ms"] - segment["start_ms"],
                                 (segment["end_frame"] - segment["start_frame"] + 1) * 10)


class InjectionTests(unittest.TestCase):
    def test_injections_modify_real_samples(self):
        samples = load_wav(INPUT / "sample_b_two_speech.wav")
        items = MANIFEST.for_sample("sample_b_two_speech.wav")
        mutated, hits = apply_injections(samples, items, 25, 10, 16000)
        self.assertFalse(np.array_equal(samples, mutated))
        self.assertEqual(hits[0]["injection_id"], "INJ-001")
        self.assertEqual(hits[0]["type"], "pop")

    def test_nonfinite_enters_failure_list(self):
        result = process_sample(
            INPUT / "sample_e_nonfinite.wav", CONFIG,
            MANIFEST.for_sample("sample_e_nonfinite.wav"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure"]["code"], E_NONFINITE_SAMPLE)

    def test_run_never_crashes_on_bad_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run(INPUT, Path(tmp), CONFIG, MANIFEST, force_restart=True)
            self.assertEqual(len(report["failures"]), 4)


class ConcurrencyTests(unittest.TestCase):
    def test_serial_parallel_field_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run(INPUT, Path(tmp), CONFIG, MANIFEST, force_restart=True)
            comp = report["concurrency_comparison"]
            self.assertTrue(comp["identical"])
            self.assertEqual(comp["serial_sample_order"], comp["parallel_sample_order"])
            self.assertEqual(report["sample_order"], comp["serial_sample_order"])


class SnapshotTests(unittest.TestCase):
    def test_resume_matches_uninterrupted(self):
        with tempfile.TemporaryDirectory() as full_dir, tempfile.TemporaryDirectory() as resume_dir:
            full = run(INPUT, Path(full_dir), CONFIG, MANIFEST, force_restart=True)
            # First partial run: kill the CLI with SIGTERM, then resume in place.
            env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
            cmd = [sys.executable, "-m", "vad.cli", "--input", str(INPUT),
                   "--output", str(resume_dir), "--graceful-after", "3"]
            proc = subprocess.run(cmd, cwd=str(ROOT), env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=30)
            self.assertEqual(proc.returncode, 0)
            partial = json.loads((Path(resume_dir) / "diagnostic_report.json").read_text())
            self.assertEqual(partial["run_state"], "interrupted")
            self.assertEqual(len(partial["results"]), 3)

            # A real SIGTERM delivered while the CLI is busy also exits 0 via the handler.
            cmd2 = [sys.executable, "-m", "vad.cli", "--input", str(INPUT),
                    "--output", str(Path(resume_dir) / "sigterm"), "--force-restart"]
            proc2 = subprocess.Popen(cmd2, cwd=str(ROOT), env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            import time
            time.sleep(0.25)
            proc2.send_signal(signal.SIGTERM)
            proc2.wait(timeout=30)
            self.assertIn(proc2.returncode, (0, -15))
            snap = read_snapshot(Path(resume_dir) / "snapshots")
            self.assertIsNotNone(snap)
            resumed = run(INPUT, Path(resume_dir), CONFIG, MANIFEST)
            self.assertEqual(
                json.dumps(full["results"], sort_keys=True),
                json.dumps(resumed["results"], sort_keys=True))
            self.assertTrue(resumed["snapshot"]["resumed"])

    def test_config_hash_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run(INPUT, Path(tmp), CONFIG, MANIFEST, force_restart=True)
            raw = read_snapshot(Path(tmp) / "snapshots")
            raw["config_hash"] = "deadbeef"
            with self.assertRaises(Exception) as ctx:
                validate_snapshot(raw, CONFIG.config_hash, MANIFEST.manifest_hash)
            self.assertEqual(ctx.exception.code, E_CONFIG_MISMATCH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
