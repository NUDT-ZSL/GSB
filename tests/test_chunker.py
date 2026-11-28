from __future__ import annotations

import hashlib
import unittest

from metering_store.chunker import chunk_bytes, chunk_parameters
from metering_store.generator import generate_dataset
from metering_store.verification import reference_chunks


class ChunkerTests(unittest.TestCase):
    def test_chunks_cover_stream_and_have_sha256_digests(self) -> None:
        dataset = generate_dataset(seed=7)
        data = dataset.files["f-0001"].data
        chunks = chunk_bytes(data, 384, 7)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0].offset, 0)
        expected_offset = 0
        for index, chunk in enumerate(chunks):
            self.assertEqual(chunk.index, index)
            self.assertEqual(chunk.offset, expected_offset)
            self.assertEqual(chunk.end_offset, chunk.offset + chunk.length)
            self.assertEqual(chunk.data, data[chunk.offset : chunk.end_offset])
            self.assertEqual(
                chunk.digest,
                hashlib.sha256(chunk.data).hexdigest(),
            )
            expected_offset = chunk.end_offset
        self.assertEqual(expected_offset, len(data))

    def test_common_record_prefix_has_identical_chunks(self) -> None:
        dataset = generate_dataset(seed=11)
        prefix = dataset.all_base_records[:6000]
        first = prefix + dataset.files["f-0001"].data[:1200]
        second = prefix + dataset.files["f-0005"].data[:1400]
        first_chunks = chunk_bytes(first, 384, 7)
        second_chunks = chunk_bytes(second, 384, 7)
        common_first = [
            (chunk.offset, chunk.length, chunk.digest)
            for chunk in first_chunks
            if chunk.end_offset <= len(prefix)
        ]
        common_second = [
            (chunk.offset, chunk.length, chunk.digest)
            for chunk in second_chunks
            if chunk.end_offset <= len(prefix)
        ]
        self.assertGreaterEqual(len(common_first), 2)
        self.assertEqual(common_first, common_second)

    def test_independent_reference_chunker_matches(self) -> None:
        dataset = generate_dataset(seed=13)
        for file_id in ("f-0001", "f-0003", "f-0005"):
            data = dataset.files[file_id].data
            for target_size, mask_bits in ((128, 5), (384, 7), (768, 9), (1536, 10)):
                mapped = [
                    (chunk.offset, chunk.length, chunk.digest)
                    for chunk in chunk_bytes(data, target_size, mask_bits)
                ]
                reference = [
                    (chunk.offset, chunk.length, chunk.digest)
                    for chunk in reference_chunks(data, target_size, mask_bits)
                ]
                self.assertEqual(mapped, reference)

    def test_parameters_and_empty_input(self) -> None:
        self.assertEqual(chunk_bytes(b"", 384, 7), [])
        self.assertEqual(chunk_parameters(16, 1)[0], 8)
        with self.assertRaises(ValueError):
            chunk_bytes(b"x", 15, 7)
        with self.assertRaises(ValueError):
            chunk_bytes(b"x", 384, 0)
        with self.assertRaises(ValueError):
            chunk_bytes(b"x", 384, 25)


if __name__ == "__main__":
    unittest.main()
