import unittest

import numpy as np

from blocksnap.bitmap import Bitmap, bitmap_byte_length
from blocksnap.errors import SnapshotError, VolumeError


class BitmapTests(unittest.TestCase):
    def test_byte_length(self):
        self.assertEqual(bitmap_byte_length(1), 1)
        self.assertEqual(bitmap_byte_length(8), 1)
        self.assertEqual(bitmap_byte_length(9), 2)

    def test_mark_and_round_trip(self):
        bitmap = Bitmap(20)
        for index in (0, 7, 8, 13, 19):
            bitmap.mark(index)
        self.assertEqual(bitmap.set_indices().tolist(), [0, 7, 8, 13, 19])
        self.assertTrue(bitmap.is_set(13))
        self.assertFalse(bitmap.is_set(12))
        self.assertEqual(bitmap.changed_count(), 5)

    def test_length_incompatible(self):
        with self.assertRaises(SnapshotError):
            Bitmap(16, np.zeros(3, dtype=np.uint8))

    def test_padding_bits_rejected(self):
        data = np.frombuffer(bytes([0x00, 0xFF]), dtype=np.uint8).copy()
        with self.assertRaises(SnapshotError):
            Bitmap(9, data)

    def test_index_bounds(self):
        with self.assertRaises(VolumeError):
            Bitmap(8).mark(8)


if __name__ == "__main__":
    unittest.main()
