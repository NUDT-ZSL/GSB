import os
import tempfile
import unittest

from blocksnap.errors import VolumeError
from blocksnap.volume import Volume, deterministic_block


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.image = os.path.join(self.tmp, "v.bin")
        self.volume = Volume.create(self.image, 4096, 16)

    def tearDown(self):
        self.volume.close()

    def test_geometry_and_deterministic_fill(self):
        self.assertEqual(self.volume.block_size, 4096)
        self.assertEqual(self.volume.block_count, 16)
        self.assertEqual(os.path.getsize(self.image), 4096 * 16)
        self.assertEqual(self.volume.read_block(0), deterministic_block(0, 4096))

    def test_unsupported_block_size_rejected(self):
        bad = os.path.join(self.tmp, "bad.bin")
        with self.assertRaises(VolumeError) as ctx:
            Volume.create(bad, 1000, 4)
        self.assertEqual(ctx.exception.code, "E_VOLUME")
        self.assertIn("location", ctx.exception.context)

    def test_length_mismatch_rejected(self):
        with open(self.image, "ab") as handle:
            handle.write(b"x")
        self.volume.close()
        with self.assertRaises(VolumeError) as ctx:
            Volume.load(self.image)
        self.assertEqual(ctx.exception.code, "E_VOLUME")
        self.assertEqual(ctx.exception.context["actual_length"], 4096 * 16 + 1)

    def test_block_index_bounds(self):
        for bad_index in (-1, 16, 100):
            with self.assertRaises(VolumeError) as ctx:
                self.volume.read_block(bad_index)
            self.assertEqual(ctx.exception.code, "E_VOLUME")

    def test_hex_injection_validation(self):
        self.volume.inject_hex(2, "deadbeef")
        block = self.volume.read_block(2)
        self.assertEqual(block[:4], bytes.fromhex("deadbeef"))
        with self.assertRaises(VolumeError):
            self.volume.inject_hex(0, "not-hex")
        with self.assertRaises(VolumeError):
            self.volume.inject_hex(0, "ff" * (4096 + 1))

    def test_write_wrong_length_rejected(self):
        with self.assertRaises(VolumeError):
            self.volume.write_block(0, b"short")


if __name__ == "__main__":
    unittest.main()
