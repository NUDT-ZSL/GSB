"""按语言独立构建的倒排索引与确定性 BM25 打分。"""

import math
from collections import Counter


class InvertedIndex:
    """单语言倒排索引：词项 -> 文档列表、词频、文档频率与长度统计。"""

    def __init__(self, language):
        self.language = language
        self.doc_lengths = {}
        self.term_freq = {}      # term -> {doc_id: tf}
        self.doc_freq = Counter()
        self.average_length = 0.0
        self.doc_count = 0

    def add_document(self, doc_id, tokens):
        counts = Counter(tokens)
        self.doc_lengths[doc_id] = len(tokens)
        for term, freq in counts.items():
            self.term_freq.setdefault(term, {})[doc_id] = freq
            self.doc_freq[term] += 1

    def finalize(self):
        self.doc_count = len(self.doc_lengths)
        total_length = sum(self.doc_lengths.values())
        self.average_length = (
            total_length / self.doc_count if self.doc_count else 0.0
        )

    def bm25_scores(self, terms, k1, b):
        """对给定词项集合计算 BM25 分项分，返回 {doc_id: {term: score}} 与总分。"""
        detail = {}
        totals = Counter()
        for term in terms:
            postings = self.term_freq.get(term)
            if not postings:
                continue
            df = self.doc_freq[term]
            idf = math.log(1.0 + (self.doc_count - df + 0.5) / (df + 0.5))
            for doc_id, freq in postings.items():
                length = self.doc_lengths[doc_id]
                denom = freq + k1 * (
                    1.0 - b + b * (length / self.average_length if self.average_length else 0.0)
                )
                score = idf * (freq * (k1 + 1.0)) / denom
                detail.setdefault(doc_id, {})[term] = round(score, 9)
                totals[doc_id] += score
        return detail, {doc_id: round(score, 9) for doc_id, score in totals.items()}

    def to_state(self):
        return {
            "language": self.language,
            "doc_lengths": dict(sorted(self.doc_lengths.items())),
            "term_freq": {
                term: dict(sorted(postings.items()))
                for term, postings in sorted(self.term_freq.items())
            },
            "doc_freq": dict(sorted(self.doc_freq.items())),
            "average_length": self.average_length,
            "doc_count": self.doc_count,
        }

    @classmethod
    def from_state(cls, state):
        index = cls(state["language"])
        index.doc_lengths = dict(state["doc_lengths"])
        index.term_freq = {term: dict(postings) for term, postings in state["term_freq"].items()}
        index.doc_freq = Counter(state["doc_freq"])
        index.average_length = state["average_length"]
        index.doc_count = state["doc_count"]
        return index


class MultiLanguageIndex:
    """每个语言标签一个独立 InvertedIndex。"""

    def __init__(self, languages):
        self.indexes = {lang: InvertedIndex(lang) for lang in languages}

    def add_document(self, doc):
        index = self.indexes[doc["detected_lang"]]
        index.add_document(doc["doc_id"], doc["tokens"])

    def finalize(self):
        for index in self.indexes.values():
            index.finalize()

    def get(self, language):
        return self.indexes[language]

    def to_state(self):
        return {lang: index.to_state() for lang, index in sorted(self.indexes.items())}

    @classmethod
    def from_state(cls, state):
        wrapper = cls([])
        wrapper.indexes = {
            lang: InvertedIndex.from_state(lang_state)
            for lang, lang_state in sorted(state.items())
        }
        return wrapper
