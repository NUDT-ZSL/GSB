import unittest

import numpy as np

from panseg.errors import E_RLE_OUT_OF_RANGE, PipelineError
from panseg.rle import (
    bitmap_to_runs,
    decode_runs,
    decode_varints,
    encode_runs,
    encode_varint,
    normalize_runs,
    packed_counts_to_runs,
    runs_to_bitmap,
    runs_to_packed_counts,
)


class RleTests(unittest.TestCase):
    def test_varint_roundtrip(self):
        for value in (0, 1, 127, 128, 16383, 16384, 2**32, 2**63):
            self.assertEqual(decode_varints(encode_varint(value)), [value])

    def test_both_compression_levels_reversible(self):
        bitmap = np.zeros((6, 5), dtype=np.uint8)
        bitmap[1:3, 1:4] = 1
        bitmap[4, 4] = 1
        runs, pixels = bitmap_to_runs(bitmap)
        for compression in (0, 1):
            payload = encode_runs(runs, compression)
            decoded_runs, decoded_pixels = decode_runs(payload, compression, bitmap.size)
            self.assertEqual(decoded_runs, runs)
            self.assertEqual(decoded_pixels, pixels)
            np.testing.assert_array_equal(runs_to_bitmap(decoded_runs, 6, 5), bitmap)

    def test_packed_counts_roundtrip(self):
        runs, _ = bitmap_to_runs(np.array([[0, 1, 1, 0], [1, 1, 0, 0]], dtype=np.uint8))
        counts = runs_to_packed_counts(runs)
        decoded, _ = packed_counts_to_runs(counts, 8)
        self.assertEqual(decoded, runs)

    def test_out_of_range_pair_rle(self):
        with self.assertRaises(PipelineError) as ctx:
            normalize_runs([(10, 5)], 12)
        self.assertEqual(ctx.exception.code, E_RLE_OUT_OF_RANGE)

    def test_out_of_range_packed_rle(self):
        with self.assertRaises(PipelineError) as ctx:
            packed_counts_to_runs([2, 99], 10)
        self.assertEqual(ctx.exception.code, E_RLE_OUT_OF_RANGE)

    def test_overlapping_runs_rejected(self):
        with self.assertRaises(PipelineError) as ctx:
            normalize_runs([(0, 4), (2, 3)], 10)
        self.assertEqual(ctx.exception.code, E_RLE_OUT_OF_RANGE)


if __name__ == "__main__":
    unittest.main()
