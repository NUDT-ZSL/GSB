import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from mocap_audio.errors import FormatError, MissingError
from mocap_audio.wavio import decode_wav, read_header, write_wav
from tests._helpers import make_tone


class WavIoTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_roundtrip_16_and_24_bit(self):
        for bits in (16, 24):
            data = make_tone(4800, 2, seed=bits) * 0.5
            path = os.path.join(self.tmp, "f%s.wav" % bits)
            info = write_wav(path, data, 48000, bits)
            self.assertEqual((info.frames, info.bits, info.channels),
                             (4800, bits, 2))
            header = read_header(path)
            decoded_info, decoded = decode_wav(path)
            self.assertEqual(header.sample_rate, 48000)
            self.assertEqual(decoded.shape, (4800, 2))
            tolerance = 1.0 / (2 ** (bits - 1))
            self.assertLessEqual(np.max(np.abs(decoded - data)), tolerance)

    def test_24bit_little_endian_three_bytes(self):
        path = os.path.join(self.tmp, "p24.wav")
        write_wav(path, np.array([[0.5], [-0.5]]), 48000, 24)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(b"fmt ", raw)
        self.assertIn(b"data", raw)
        _, decoded = decode_wav(path)
        self.assertAlmostEqual(decoded[0, 0], 0.5, delta=1 / 8388608)
        self.assertAlmostEqual(decoded[1, 0], -0.5, delta=1 / 8388608)

    def test_missing_and_bad_header(self):
        with self.assertRaises(MissingError):
            read_header(os.path.join(self.tmp, "nope.wav"))
        bad = os.path.join(self.tmp, "bad.wav")
        with open(bad, "wb") as fh:
            fh.write(b"not a wav file")
        with self.assertRaises(FormatError):
            read_header(bad)


if __name__ == "__main__":
    unittest.main()

