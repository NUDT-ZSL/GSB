import os
import tempfile
import unittest

from oswb.chunker import build_manifest, plan_chunks
from oswb.errors import BusinessError
from oswb.sample import generate_sample


class ChunkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "sample.bin")
        generate_sample(self.path, 10_000)

    def tearDown(self):
        self.temp.cleanup()

    def test_deterministic_manifest(self):
        first = build_manifest(self.path, 1024)
        second = build_manifest(self.path, 1024)
        self.assertEqual(first, second)
        self.assertEqual(first["total_chunks"], 10)
        self.assertEqual(sum(item["length"] for item in first["chunks"]), 10_000)

    def test_chunk_size_sequences_cover_file(self):
        plan = plan_chunks(10_000, 3000, "sha256")
        self.assertEqual([item[0] for item in plan], [0, 1, 2, 3])
        self.assertEqual([item[2] for item in plan], [3000, 3000, 3000, 1000])

    def test_empty_file_single_empty_chunk(self):
        empty = os.path.join(self.temp.name, "empty.bin")
        open(empty, "wb").close()
        manifest = build_manifest(empty, 4096)
        self.assertEqual(manifest["total_chunks"], 1)
        self.assertEqual(manifest["chunks"][0]["length"], 0)

    def test_illegal_chunk_sizes(self):
        for bad in (0, -1, 10_001):
            with self.assertRaises(BusinessError) as context:
                build_manifest(self.path, bad)
            self.assertEqual(context.exception.code, "E_CHUNK")

    def test_illegal_chunk_creates_no_files(self):
        before = set(os.listdir(self.temp.name))
        with self.assertRaises(BusinessError):
            build_manifest(self.path, 99_999)
        self.assertEqual(before, set(os.listdir(self.temp.name)))


if __name__ == "__main__":
    unittest.main()
