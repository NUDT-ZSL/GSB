import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.errors import FormatError, MissingError, RateError
from mocap_audio.session import load_session
from mocap_audio.wavio import write_wav
from tests._helpers import base_session, make_tone


def build_valid(tmp):
    ref_path = os.path.join(tmp, "wav", "ref.wav")
    dev_path = os.path.join(tmp, "wav", "dev.wav")
    os.makedirs(os.path.join(tmp, "wav"), exist_ok=True)
    write_wav(ref_path, make_tone(4800, 2), 48000, 24)
    write_wav(dev_path, make_tone(4410, 2), 44100, 16)
    ref = {
        "device_id": "REF", "sample_rate": 48000, "bits": 24,
        "is_reference": True,
        "segments": [{
            "segment_id": "S1", "file": "wav/ref.wav",
            "start_ms": 0, "duration_ms": 100,
            "channels": ["A", "B"], "gain": [1.0, 1.0],
            "pulses": [{"theoretical_ms": 20, "search_ms": 5},
                       {"theoretical_ms": 60, "search_ms": 5}]}],
    }
    dev = {
        "device_id": "D1", "sample_rate": 44100, "bits": 16,
        "segments": [{
            "segment_id": "S2", "file": "wav/dev.wav",
            "start_ms": 100, "duration_ms": 100,
            "channels": ["A", "B"], "gain": [1.0, 0.5],
            "pulses": [{"theoretical_ms": 30, "search_ms": 5}]}],
    }
    return base_session(tmp, [ref, dev])


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = build_valid(self.tmp)

    def _mutate(self, mutator):
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        mutator(raw)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_valid_session_loads(self):
        session = load_session(self.path)
        self.assertEqual(session.reference_device_id, "REF")
        self.assertEqual(len(list(session.iter_segments())), 2)

    def test_rate_errors(self):
        self._mutate(lambda r: r.update(target_sample_rate=22050))
        with self.assertRaises(RateError) as ctx:
            load_session(self.path)
        self.assertEqual(ctx.exception.code, "E_RATE")

        self.setUp()
        self._mutate(lambda r: r["devices"][1].update(sample_rate=32000))
        with self.assertRaises(RateError):
            load_session(self.path)

        self.setUp()
        self._mutate(lambda r: r.update(target_bits=32))
        with self.assertRaises(RateError):
            load_session(self.path)

    def test_format_errors(self):
        self._mutate(lambda r: r["devices"][0]["segments"][0].update(
            start_ms=-1))
        with self.assertRaises(FormatError) as ctx:
            load_session(self.path)
        self.assertIn("REF", ctx.exception.refs)

        self.setUp()
        self._mutate(lambda r: r["devices"][0]["segments"][0]["pulses"]
                     .__setitem__(1, {"theoretical_ms": 20, "search_ms": 5}))
        with self.assertRaises(FormatError):
            load_session(self.path)

        self.setUp()
        self._mutate(lambda r: r["devices"][1]["segments"][0].update(
            gain=[1.0]))
        with self.assertRaises(FormatError):
            load_session(self.path)

        self.setUp()
        self._mutate(lambda r: r.update(model_order=3))
        with self.assertRaises(FormatError):
            load_session(self.path)

        self.setUp()
        self._mutate(lambda r: r.update(stat_window_ms=0))
        with self.assertRaises(FormatError):
            load_session(self.path)

        self.setUp()
        self._mutate(lambda r: r["devices"][0]["segments"][0].update(
            gain=[1.0, float("nan")]))
        with self.assertRaises(FormatError):
            load_session(self.path)

    def test_missing_errors(self):
        with self.assertRaises(MissingError):
            load_session(os.path.join(self.tmp, "absent.json"))

        self._mutate(lambda r: r["devices"][1]["segments"][0].update(
            file="wav/gone.wav"))
        with self.assertRaises(MissingError) as ctx:
            load_session(self.path)
        self.assertEqual(ctx.exception.code, "E_MISSING")

    def test_pulse_outside_segment(self):
        self._mutate(lambda r: r["devices"][0]["segments"][0]["pulses"]
                     .__setitem__(0, {"theoretical_ms": 99, "search_ms": 5}))
        with self.assertRaises(FormatError):
            load_session(self.path)


if __name__ == "__main__":
    unittest.main()

