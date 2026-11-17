"""归一化、分词、语言识别、翻译桥接与 BM25 的单元测试。"""

import math
import unittest

from xling_pipeline.bridge import build_translation_index, expand_query
from xling_pipeline.config import canonical_json_bytes, config_hash
from xling_pipeline.index import MultiLanguageIndex
from xling_pipeline.loader import normalize_corpus, build_tokenizer
from xling_pipeline.text import fold_fullwidth, normalize_text, Tokenizer


RULES = {"case_fold": True, "fold_fullwidth": True, "merge_whitespace": True}


class TextTests(unittest.TestCase):
    def test_fullwidth_fold(self):
        self.assertEqual(fold_fullwidth("ＡＢＣ１２３"), "ABC123")
        self.assertEqual(normalize_text("  Hello\u3000World  ", RULES), "hello world")

    def test_tokenizer_fmm_and_stopwords(self):
        tokenizer = Tokenizer({"价格", "屏幕", "price"}, {"的"})
        self.assertEqual(tokenizer.tokenize("价格的屏幕"), ["价格", "屏幕"])
        self.assertEqual(tokenizer.tokenize("price tag"), ["price", "tag"])

    def test_language_detection(self):
        config = {
            "language_identification": {
                "enabled": True,
                "cjk_script_threshold": 0.6,
                "fallback_language": "en",
            },
            "tokenization": {"stopwords": {"en": [], "zh": []}, "fillers": {"en": [], "zh": []}},
            "languages": {"allowed": ["en", "zh"]},
        }
        translations = [
            {"source": "price", "target": "价格", "confidence": 0.9},
            {"source": "价格", "target": "price", "confidence": 0.9},
        ]
        vocab = {"price", "价格"}
        from xling_pipeline.text import detect_language
        lang, evidence = detect_language("价格很合理", ["价格"], config, vocab)
        self.assertEqual(lang, "zh")
        self.assertEqual(evidence["rule"], "cjk_script_majority")
        lang, _ = detect_language("good price", ["price"], config, vocab)
        self.assertEqual(lang, "en")


class LoaderTests(unittest.TestCase):
    def _config(self):
        return {
            "languages": {"allowed": ["en", "zh"]},
            "normalization": RULES,
            "language_identification": {
                "enabled": True, "cjk_script_threshold": 0.6,
                "fallback_language": "en",
            },
            "tokenization": {
                "stopwords": {"en": ["the"], "zh": ["的"]},
                "fillers": {"en": [], "zh": []},
            },
        }

    def test_invalid_records_are_isolated(self):
        translations = [
            {"source": "price", "target": "价格", "confidence": 0.9},
            {"source": "价格", "target": "price", "confidence": 0.9},
        ]
        tokenizer = build_tokenizer(translations, self._config())
        vocab = {"price", "价格"}
        rows = [
            {"doc_id": "D1", "lang": "en", "title": "T", "body": "price"},
            {"doc_id": "", "lang": "en", "title": "T", "body": "price"},
            {"doc_id": "D1", "lang": "en", "title": "T", "body": "dup"},
            {"doc_id": "D2", "lang": "fr", "title": "T", "body": "price"},
            {"doc_id": "D3", "lang": "zh", "title": "T", "body": "  "},
        ]
        normalized, failures = normalize_corpus(
            "document", rows, self._config(), tokenizer, vocab
        )
        self.assertEqual([d["doc_id"] for d in normalized], ["D1"])
        self.assertEqual(len(failures), 4)
        self.assertTrue(all(f["code"] == "E_RECORD_INVALID" for f in failures))
        stages = {f["stage"] for f in failures}
        self.assertEqual(stages, {"normalize"})

    def test_cross_batch_duplicate_rejected(self):
        translations = [
            {"source": "price", "target": "价格", "confidence": 0.9},
        ]
        tokenizer = build_tokenizer(translations, self._config())
        seen = set()
        rows_a = [{"doc_id": "D1", "lang": "en", "title": "a", "body": "price"}]
        rows_b = [{"doc_id": "D1", "lang": "en", "title": "b", "body": "price"}]
        norm_a, fail_a = normalize_corpus("document", rows_a, self._config(), tokenizer, {"price"}, seen)
        norm_b, fail_b = normalize_corpus("document", rows_b, self._config(), tokenizer, {"price"}, seen)
        self.assertEqual(len(norm_a), 1)
        self.assertEqual(norm_b, [])
        self.assertEqual(fail_b[0]["detail"], "duplicate document id: D1")


class BridgeTests(unittest.TestCase):
    def test_confidence_order_and_suppression(self):
        pairs = [
            {"source": "price", "target": "价格", "confidence": 0.9},
            {"source": "price", "target": "价钱", "confidence": 0.6},
            {"source": "price", "target": "报价", "confidence": 0.6},
        ]
        index = build_translation_index(pairs)
        config = {"bridge": {"max_translations_per_term": 1, "decay_weight": 0.8}}
        expansions, suppressed = expand_query(["price"], index, config)
        self.assertEqual(len(expansions), 1)
        self.assertEqual(expansions[0]["target_term"], "价格")
        self.assertEqual(expansions[0]["origin"], "translation")
        suppressed_terms = [s["target_term"] for s in suppressed]
        # 同置信度按字典序：价钱 < 报价
        self.assertEqual(suppressed_terms, ["价钱", "报价"])
        self.assertTrue(all(s["reason"] == "max_translations_exceeded" for s in suppressed))


class IndexTests(unittest.TestCase):
    def test_bm25_tiebreak_by_doc_id(self):
        index = MultiLanguageIndex(["en"])
        docs = [
            {"doc_id": "D2", "detected_lang": "en", "tokens": ["price", "price"]},
            {"doc_id": "D1", "detected_lang": "en", "tokens": ["price", "price"]},
        ]
        for doc in docs:
            index.add_document(doc)
        index.finalize()
        _, totals = index.get("en").bm25_scores(["price"], 1.5, 0.75)
        self.assertEqual(totals["D1"], totals["D2"])
        self.assertTrue(math.isfinite(totals["D1"]))


class HashTests(unittest.TestCase):
    def test_hash_stable_and_sensitive(self):
        cfg = {"a": 1, "b": [2, 3], "paths": {"snapshot_dir": "/x", "documents": "/d"}}
        h1 = config_hash(cfg)
        cfg2 = dict(cfg)
        cfg2["paths"] = dict(cfg["paths"], snapshot_dir="/y")
        self.assertEqual(h1, config_hash(cfg2))  # 输出路径不影响哈希
        cfg3 = dict(cfg, a=2)
        self.assertNotEqual(h1, config_hash(cfg3))
        # 规范化序列化确定性
        self.assertEqual(canonical_json_bytes({"b": 1, "a": 2}), canonical_json_bytes({"a": 2, "b": 1}))


if __name__ == "__main__":
    unittest.main()
