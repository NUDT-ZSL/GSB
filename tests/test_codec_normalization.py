import json
import unittest

from workbench.codec import (
    LEVEL_DELTA_LOW_FREQ,
    Posting,
    chunk_postings,
    decode_block,
    decode_payload,
    decode_varint,
    encode_block,
    encode_payload,
    encode_varint,
    filter_low_frequency,
)
from workbench.normalization import (
    fold_halfwidth,
    normalize_text,
    parse_stream,
    tokenize,
)


class NormalizationTests(unittest.TestCase):
    def test_case_and_width_fold(self):
        self.assertEqual(normalize_text("  ALPHA　ｂｒａｖｏ\n\tcharlie "),
                         "alpha bravo charlie")

    def test_halfwidth_punctuation(self):
        self.assertEqual(fold_halfwidth("ＡＢＣ１２３！"), "ABC123!")

    def test_tokenize_positions(self):
        tokens = tokenize("Alpha alpha ＡＬＰＨＡ")
        self.assertEqual(tokens, ["alpha", "alpha", "alpha"])

    def test_parse_failures(self):
        lines = [
            json.dumps({"doc_id": "d1", "title": "t", "body": "alpha bravo",
                        "source": "s"}),
            "{bad json",
            json.dumps({"doc_id": "d2", "title": "t", "body": "   ",
                        "source": "s"}),
            json.dumps({"doc_id": "d1", "title": "dup",
                        "body": "alpha", "source": "s"}),
        ]
        docs, failures = parse_stream(lines)
        self.assertEqual([doc.doc_id for doc in docs], ["d1"])
        codes = [item["code"] for item in failures]
        self.assertEqual(
            codes,
            ["E_PARSE_INVALID", "E_EMPTY_DOC", "E_DUP_DOC_ID"],
        )
        self.assertEqual(docs[0].term_freq["alpha"], 1)
        self.assertEqual(docs[0].positions["bravo"], (1,))


class VarintTests(unittest.TestCase):
    def test_round_trip(self):
        for value in (0, 1, 127, 128, 16383, 16384, 2**32, 2**63):
            decoded, offset = decode_varint(encode_varint(value), 0)
            self.assertEqual(decoded, value)
            self.assertEqual(offset, len(encode_varint(value)))

    def test_negative_rejected(self):
        with self.assertRaises(Exception):
            encode_varint(-1)


class CodecTests(unittest.TestCase):
    def _postings(self):
        return [
            Posting(0, 2, (1, 4)),
            Posting(3, 1, (0,)),
            Posting(10, 3, (2, 5, 9)),
        ]

    def test_payload_round_trip_with_positions(self):
        postings = self._postings()
        blob = encode_payload(postings, store_positions=True)
        self.assertEqual(decode_payload(blob, store_positions=True), tuple(postings))

    def test_payload_round_trip_without_positions(self):
        postings = self._postings()
        blob = encode_payload(postings, store_positions=False)
        decoded = decode_payload(blob, store_positions=False)
        self.assertEqual([item.doc_seq for item in decoded], [0, 3, 10])
        self.assertTrue(all(item.positions == () for item in decoded))

    def test_block_round_trip_both_levels(self):
        postings = self._postings()
        blocks = chunk_postings("alpha", postings, block_size=2)
        self.assertEqual(len(blocks), 2)
        for level in (1, LEVEL_DELTA_LOW_FREQ):
            for store_positions in (True, False):
                blob = encode_block(blocks[0], level=level,
                                    store_positions=store_positions)
                term, chunk_index, decoded_level, has_pos, decoded = decode_block(blob)
                self.assertEqual((term, chunk_index), ("alpha", 0))
                self.assertEqual(decoded_level, level)
                self.assertEqual(has_pos, store_positions)
                if store_positions:
                    self.assertEqual(decoded, blocks[0].postings)
                else:
                    self.assertEqual(
                        [item.doc_seq for item in decoded],
                        [item.doc_seq for item in blocks[0].postings],
                    )
                    self.assertTrue(all(item.positions == () for item in decoded))

    def test_low_frequency_filter(self):
        self.assertEqual(filter_low_frequency(self._postings(), min_doc_freq=5), ())
        self.assertEqual(
            len(filter_low_frequency(self._postings(), min_doc_freq=2)), 3
        )

    def test_chunking_bounds(self):
        postings = [Posting(seq, 1, (0,)) for seq in range(10)]
        blocks = chunk_postings("t", postings, block_size=4)
        self.assertEqual([len(block.postings) for block in blocks], [4, 4, 2])


if __name__ == "__main__":
    unittest.main()
