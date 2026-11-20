"""BM25-like scoring combined with bridge confidence weighting.

The combine rule is explicit: ``weighted_sum`` sums, per document, the
confidence-weighted contributions of every (query term, candidate) hit.
Any other rule is rejected with E_CONFIG_INVALID at resource load time.
"""

import math
from math import fsum


def idf(doc_count, doc_freq):
    return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))


def bm25_raw(tf, doc_len, avg_doc_len, doc_count, doc_freq, k1, b):
    if doc_count == 0 or doc_freq == 0:
        return 0.0
    norm = tf + k1 * (1.0 - b + b * doc_len / avg_doc_len) if avg_doc_len else 1.0
    return idf(doc_count, doc_freq) * (tf * (k1 + 1.0)) / norm


def combine_contributions(weighted_values, rule):
    """Merge per-candidate weighted contributions for one document."""
    if rule == "weighted_sum":
        return fsum(weighted_values)
    raise ValueError(f"unsupported combine rule {rule!r}")


def rank(doc_hits, combine_rule, top_k):
    """Combine per-doc details and produce the deterministic top-k ranking."""
    ranked = []
    for doc_id, details in doc_hits.items():
        score = combine_contributions([d["weighted"] for d in details],
                                      combine_rule)
        ranked.append((doc_id, score, details))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:top_k]
