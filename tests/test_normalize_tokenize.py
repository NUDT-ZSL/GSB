from __future__ import annotations

import copy
import unittest

from query_understanding.dictionary import Dictionary
from query_understanding.errors import E_DICT_TRUNCATED, E_MAP_MISSING, QUError
from query_understanding.mappings import MappingTable
from query_understanding.normalize import normalize_query
from query_understanding.tokenize import tokenize_text
from query_understanding.util import canonical_json, read_json, with_checksum

from tests.common import DATA, load_engine, load_small


class NormalizeTokenizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()
        cls.queries = load_small()

    def test_traditional_fullwidth_normalization(self):
        text = "  蘋果　手機　黑色  "
        self.assertEqual(
            normalize_query(text, self.engine.mappings),
            "苹果 手机 黑色",
        )

    def test_token_offsets_are_codepoints_and_cover_text(self):
        normalized = "苹果 手机😀"
        tokens = tokenize_text(normalized, self.engine.dictionary)
        for token in tokens:
            self.assertEqual(
                normalized[token["start"] : token["end"]], token["text"]
            )
        covered = {
            index
            for token in tokens
            for index in range(token["start"], token["end"])
        }
        self.assertEqual(
            covered,
            {index for index, char in enumerate(normalized) if not char.isspace()},
        )
        self.assertEqual(tokens[-1]["text"], "😀")
        self.assertEqual(tokens[-1]["start"], normalized.index("😀"))

    def test_same_input_and_config_is_byte_identical(self):
        query = self.queries[0]
        first = self.engine.process(query, path="exact")
        second = self.engine.process(query, path="exact")
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(first["result_checksum"], second["result_checksum"])

    def test_mapping_missing_declared_source(self):
        payload = copy.deepcopy(read_json(DATA / "mappings.json"))
        payload["traditional_sources"].append("龘")
        payload = with_checksum(payload)
        with self.assertRaises(QUError) as context:
            MappingTable.from_payload(payload)
        self.assertEqual(context.exception.code, E_MAP_MISSING)

    def test_mapping_missing_fullwidth_value(self):
        payload = copy.deepcopy(read_json(DATA / "mappings.json"))
        payload["fullwidth_to_halfwidth"].pop("Ａ")
        payload = with_checksum(payload)
        table = MappingTable.from_payload(payload)
        with self.assertRaises(QUError) as context:
            table.normalize("Ａ")
        self.assertEqual(context.exception.code, E_MAP_MISSING)
        self.assertEqual(context.exception.category, "fullwidth")

    def test_dictionary_truncation_returns_business_code(self):
        with self.assertRaises(QUError) as context:
            Dictionary.from_file(DATA / "dictionary.json", max_entries=8)
        self.assertEqual(context.exception.code, E_DICT_TRUNCATED)

    def test_longest_dictionary_match(self):
        normalized = "蓝牙耳机"
        tokens = tokenize_text(normalized, self.engine.dictionary)
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["text"], normalized)
        self.assertGreaterEqual(len(tokens[0]["entry_ids"]), 1)


if __name__ == "__main__":
    unittest.main()

