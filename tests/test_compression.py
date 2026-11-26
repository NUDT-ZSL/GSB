from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from index_diag.analysis import analyze_document
from index_diag.compression import (
    CODEC_MODES,
    CompressionCache,
    CompressionParams,
    decode_integer_stream,
    encode_integer_stream,
    refresh_compression_cache,
)
from index_diag.config import load_config
from index_diag.errors import (
    DiagnosticError,
    E_COMPRESS_PARAM,
    E_SNAPSHOT_CORRUPT,
)
from index_diag.postings import postings_from_documents, postings_to_sequence

from .helpers import document, make_config, write_json


class CompressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_all_codecs_round_trip(self) -> None:
        rng = random.Random(7)
        values = [rng.randint(-100000, 100000) for _ in range(81)]
        values.extend([-(2**63), 2**63 - 1, 0, -1, 1])
        for mode in CODEC_MODES:
            for step in (1, 2, 7, 100):
                params = CompressionParams(7, mode, step)
                payload = encode_integer_stream(values, params)
                decoded = decode_integer_stream(
                    payload, params, expected_length=len(values)
                )
                self.assertEqual(decoded, values, (mode, step))

    def test_invalid_parameters(self) -> None:
        for params in (
            CompressionParams(0, "raw", 1),
            CompressionParams(4, "raw", 0),
            CompressionParams(4, "mystery", 1),
        ):
            with self.assertRaises(DiagnosticError) as caught:
                encode_integer_stream([1, 2], params)
            self.assertEqual(caught.exception.code, E_COMPRESS_PARAM)

    def test_block_hashes_and_selective_decode(self) -> None:
        config_path = self.root / "config.json"
        write_json(config_path, make_config(chunk_size=3))
        config = load_config(config_path)
        docs = [
            analyze_document(
                document(1, index, ["a"] * (index + 1)),
                config.vocabulary_set,
                source="<test>",
            )
            for index in range(1, 7)
        ]
        postings, _ = postings_from_documents(docs)
        cache, report = refresh_compression_cache(
            postings, config, reason="build"
        )
        self.assertGreater(len(cache.entries["a"].blocks), 1)
        self.assertEqual(
            cache.decode_term("a"), postings_to_sequence(postings["a"])
        )
        first = cache.decode_block("a", 0)
        status = cache.block_status("a")
        self.assertEqual(len(first), status[0]["count"])
        self.assertEqual(status[0]["content_hash"][:8], cache.entries["a"].blocks[0].content_hash[:8])

        serialized = cache.to_dict()
        serialized["entries"][0]["blocks"][0]["payload"] = "AAAA"
        with self.assertRaises(DiagnosticError) as caught:
            CompressionCache.from_dict(serialized)
        self.assertEqual(caught.exception.code, E_SNAPSHOT_CORRUPT)

    def test_directed_suffix_reuse_and_cache_key_rehoming(self) -> None:
        config1_path = self.root / "config1.json"
        config2_path = self.root / "config2.json"
        config3_path = self.root / "config3.json"
        write_json(config1_path, make_config(generation=1, chunk_size=2))
        write_json(config2_path, make_config(generation=2, chunk_size=2))
        write_json(config3_path, make_config(generation=2, chunk_size=3))
        config1 = load_config(config1_path)
        config2 = load_config(config2_path)
        config3 = load_config(config3_path)

        docs = [
            analyze_document(
                document(1, index, ["a", "b"]),
                config1.vocabulary_set,
                source="<test>",
            )
            for index in range(1, 11)
        ]
        postings, _ = postings_from_documents(docs)
        cache1, _ = refresh_compression_cache(postings, config1, reason="build")

        changed_docs = list(docs)
        changed_docs[-1] = analyze_document(
            document(1, 10, ["a", "b", "b"]),
            config1.vocabulary_set,
            source="<test>",
        )
        changed_postings, _ = postings_from_documents(changed_docs)
        cache2, report2 = refresh_compression_cache(
            changed_postings,
            config1,
            previous=cache1,
            affected_terms={"a", "b"},
            reason="batch_replacement",
        )
        self.assertGreater(report2["blocks_reused"], 0)
        self.assertGreater(report2["blocks_recomputed"], 0)
        self.assertGreater(next(iter(report2["first_recomputed_block"].values())), 0)
        self.assertEqual(
            set(report2["untouched_terms"]), set(postings) - {"a", "b"}
        )

        cache3, report3 = refresh_compression_cache(
            changed_postings,
            config2,
            previous=cache2,
            affected_terms=None,
            reason="config_change",
        )
        self.assertEqual(report3["blocks_recomputed"], 0)
        self.assertEqual(len(report3["rekeyed_terms"]), len(changed_postings))
        self.assertEqual(
            set(entry.cache_key for entry in cache3.entries.values()),
            {
                entry.expected_cache_key()
                for entry in cache3.entries.values()
            },
        )

        cache4, report4 = refresh_compression_cache(
            changed_postings,
            config3,
            previous=cache3,
            affected_terms=None,
            reason="config_change",
        )
        self.assertGreater(report4["blocks_recomputed"], 0)
        self.assertEqual(report4["blocks_reused"], 0)
        self.assertEqual(cache4.decode_term("a"), cache3.decode_term("a"))


if __name__ == "__main__":
    unittest.main()
