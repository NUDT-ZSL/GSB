"""Unit and end-to-end tests with deterministic local fixtures."""

import json
import os
import sys
import unittest

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multitool import pcm, resample
from multitool.engine import process
from multitool.errors import (
    E_EMPTY,
    E_FORMAT,
    E_MISSING,
    E_OK,
    E_RATE,
    E_SCHEMA,
    E_WRITE,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAV_DIR = os.path.join(ROOT, "samples", "wav")


class TestPCM(unittest.TestCase):
    def test_s24_hand_parser_roundtrip(self):
        values = np.array([-8388608, -1, 0, 1, 8388607], dtype=np.int32)
        raw = b"".join(
            int(v).to_bytes(3, byteorder="little", signed=True) for v in values
        )
        decoded = pcm._decode_s24_le(raw)
        np.testing.assert_array_equal(decoded, values)

    def test_read_24bit_fullscale_flags_clip(self):
        path = os.path.join(WAV_DIR, "track_impulse_24.wav")
        samples, rate, bits, clipped = pcm.read_wav_mono(path)
        self.assertEqual(rate, 44100)
        self.assertEqual(bits, 24)
        self.assertTrue(clipped)
        self.assertAlmostEqual(samples[100], -1.0, places=6)
        self.assertEqual(samples[0], 0.0)

    def test_read_rejects_stereo_and_bad_rate(self):
        for name in ("bad_stereo.wav", "bad_rate.wav", "bad_32bit.wav"):
            with self.assertRaises(pcm.TrackFormatError) as ctx:
                pcm.read_wav_mono(os.path.join(WAV_DIR, name))
            self.assertEqual(ctx.exception.code, E_FORMAT)


class TestResample(unittest.TestCase):
    def test_output_length_rounding(self):
        for n, fs_in, fs_out in [
            (4000, 8000, 44100),
            (8000, 16000, 44100),
            (11025, 44100, 16000),
            (1234, 44100, 8000),
        ]:
            out = resample.resample(np.zeros(n), fs_in, fs_out)
            expected = resample.expected_output_frames(n, fs_in, fs_out)
            self.assertEqual(out.shape[0], expected)

    def test_impulse_stays_on_frame_zero(self):
        impulse = np.zeros(500)
        impulse[0] = 1.0
        for fs_in, fs_out in [(8000, 44100), (16000, 44100), (44100, 8000)]:
            out = resample.resample(impulse, fs_in, fs_out)
            self.assertEqual(int(np.argmax(np.abs(out))), 0)

    def test_dc_gain_preserved(self):
        dc = np.full(2000, 0.5)
        out = resample.resample(dc, 8000, 44100)
        # Interior samples stay flat; trim enough to clear both filter
        # boundaries (half width ~ 88 input frames -> ~ 485 output frames).
        interior = out[600:-600]
        self.assertLess(np.max(np.abs(interior - 0.5)), 1e-9)

    def test_passthrough_same_rate(self):
        x = np.linspace(-1, 1, 300)
        out = resample.resample(x, 44100, 44100)
        np.testing.assert_array_equal(out, x)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(ROOT, "samples", "tmp_test")
        import shutil

        if os.path.isdir(self.tmp):
            shutil.rmtree(self.tmp)
        os.makedirs(self.tmp, exist_ok=True)

    def _manifest(
        self, tracks, rate=44100, bits=16, out="mix.wav", rep="report.json"
    ):
        path = os.path.join(self.tmp, "manifest.json")
        out_abs = out if os.path.isabs(out) else os.path.join(self.tmp, out)
        rep_abs = rep if os.path.isabs(rep) else os.path.join(self.tmp, rep)
        data = {
            "target_sample_rate": rate,
            "target_bit_depth": bits,
            "output": out_abs,
            "report": rep_abs,
            "tracks": tracks,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_happy_path_alignment_and_report(self):
        manifest_path = os.path.join(ROOT, "samples", "timeline.json")
        import shutil

        out_dir = os.path.join(ROOT, "samples", "out")
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        report, code = process(manifest_path)
        self.assertEqual(code, E_OK)
        mix_path = os.path.join(ROOT, "samples", "out", "mix.wav")
        info = sf.info(mix_path)
        self.assertEqual(info.samplerate, 44100)
        self.assertEqual(info.frames, report["output_frames"])
        self.assertEqual(info.subtype, "PCM_16")

        # Sample-level impulse placement on an isolated manifest: input
        # impulse at frame 100, offset 5000 -> spike exactly at frame 5100.
        isolated = os.path.join(self.tmp, "isolated.json")
        iso_mix = os.path.join(self.tmp, "isolated_mix.wav")
        iso_report = os.path.join(self.tmp, "isolated_report.json")
        with open(isolated, "w") as handle:
            json.dump(
                {
                    "target_sample_rate": 44100,
                    "target_bit_depth": 16,
                    "output": iso_mix,
                    "report": iso_report,
                    "tracks": [
                        {
                            "file": os.path.join(WAV_DIR, "track_impulse_24.wav"),
                            "offset": 5000,
                            "gain": 1.0,
                        }
                    ],
                },
                handle,
            )
        iso_report_dict, iso_code = process(isolated)
        self.assertEqual(iso_code, E_OK)
        data, _ = sf.read(iso_mix)
        self.assertEqual(int(np.argmax(np.abs(data))), 5100)
        self.assertEqual(iso_report_dict["tracks"][0]["start_frame"], 5000)

        # Negative-offset track begins at frame 0; report agrees with truth.
        neg = report["tracks"][2]
        self.assertEqual(neg["start_frame"], 0)
        self.assertEqual(neg["end_frame"], 10825)

        # Frame values in report match the target-rate ground truth.
        self.assertEqual(
            report["tracks"][0]["converted_frames"],
            resample.expected_output_frames(4000, 8000, 44100),
        )

    def test_version_match_keeps_old_files(self):
        manifest_path = self._manifest(
            [
                {
                    "file": os.path.join(WAV_DIR, "track_44k_16.wav"),
                    "offset": 0,
                    "gain": 1,
                }
            ]
        )
        report1, _ = process(manifest_path)
        self.assertTrue(report1["output_replaced"])
        report2, code = process(manifest_path)
        self.assertEqual(code, E_OK)
        self.assertFalse(report2["output_replaced"])

    def test_format_error_isolated_per_track(self):
        tracks = [
            {"file": os.path.join(WAV_DIR, "bad_rate.wav"), "offset": 0, "gain": 1},
            {"file": os.path.join(WAV_DIR, "bad_stereo.wav"), "offset": 0, "gain": 1},
            {
                "file": os.path.join(WAV_DIR, "track_44k_16.wav"),
                "offset": 0,
                "gain": 1,
            },
        ]
        report, code = process(self._manifest(tracks))
        self.assertEqual(code, E_FORMAT)
        statuses = [t["status"] for t in report["tracks"]]
        self.assertEqual(statuses[:2], [E_FORMAT, E_FORMAT])
        self.assertEqual(statuses[2], E_OK)
        self.assertEqual(report["output_frames"], 11025)

    def test_missing_track_and_missing_manifest(self):
        tracks = [
            {"file": os.path.join(self.tmp, "nope.wav"), "offset": 0, "gain": 1}
        ]
        report, code = process(self._manifest(tracks))
        self.assertEqual(code, E_EMPTY)
        self.assertEqual(report["tracks"][0]["status"], E_MISSING)

        report, code = process(os.path.join(self.tmp, "absent.json"))
        self.assertEqual(code, E_MISSING)

    def test_schema_and_rate_codes(self):
        bad_schema = os.path.join(self.tmp, "bad_schema.json")
        with open(bad_schema, "w") as handle:
            json.dump({"target_sample_rate": 44100}, handle)
        _, code = process(bad_schema)
        self.assertEqual(code, E_SCHEMA)

        bad_rate = self._manifest(
            [
                {
                    "file": os.path.join(WAV_DIR, "track_44k_16.wav"),
                    "offset": 0,
                    "gain": 1,
                }
            ],
            rate=22050,
        )
        _, code = process(bad_rate)
        self.assertEqual(code, E_RATE)

    def test_empty_after_negative_offset(self):
        tracks = [
            {
                "file": os.path.join(WAV_DIR, "track_44k_16.wav"),
                "offset": -99999,
                "gain": 1,
            }
        ]
        report, code = process(self._manifest(tracks))
        self.assertEqual(code, E_EMPTY)

    def test_soft_limiter_only_on_final_mix(self):
        # Two full-scale tracks sum to 2.0 pre-limit and stay below 1 after.
        imp = np.zeros(100)
        imp[10] = 1.0
        path = os.path.join(self.tmp, "imp.wav")
        sf.write(path, imp, 44100, subtype="PCM_24")
        tracks = [
            {"file": path, "offset": 0, "gain": 1.0},
            {"file": path, "offset": 0, "gain": 1.0},
        ]
        report, code = process(self._manifest(tracks))
        self.assertEqual(code, E_OK)
        self.assertAlmostEqual(report["mix_peak_pre_limit"], 2.0, places=5)
        self.assertLess(report["mix_peak_final"], 1.0)
        # Per-track peak is measured after gain but before mixing.
        self.assertAlmostEqual(
            report["tracks"][0]["post_gain_peak"], 1.0, places=5
        )

    def test_write_error_keeps_previous_output(self):
        good = self._manifest(
            [
                {
                    "file": os.path.join(WAV_DIR, "track_44k_16.wav"),
                    "offset": 0,
                    "gain": 1,
                }
            ]
        )
        report1, code1 = process(good)
        self.assertEqual(code1, E_OK)
        wav_path = os.path.join(self.tmp, "mix.wav")
        old_size = os.path.getsize(wav_path)

        # Point the WAV at a path whose parent cannot be created to force E_WRITE.
        broken = os.path.join(self.tmp, "broken.json")
        with open(good) as src:
            data = json.load(src)
        data["output"] = os.path.join(self.tmp, "readonly_xyz", "mix.wav")
        with open(broken, "w") as handle:
            json.dump(data, handle)
        # Make the parent directory creation fail deterministically.
        blocker = os.path.join(self.tmp, "readonly_xyz")
        with open(blocker, "w") as handle:
            handle.write("not a directory")
        report2, code2 = process(broken)
        self.assertEqual(code2, E_WRITE)
        self.assertTrue(os.path.isfile(wav_path))
        self.assertEqual(os.path.getsize(wav_path), old_size)


if __name__ == "__main__":
    unittest.main(verbosity=2)
