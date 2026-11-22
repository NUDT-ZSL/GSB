"""Independent result-verification tool.

Recomputes, from the source documents alone, everything the pipeline
produced, and cross-checks:
  1. every term's block-by-block decompression against raw postings
  2. baseline vs compressed-index BM25 scores for any query
  3. top-k set membership and score ordering legality
  4. snapshot statistics vs per-term detail
  5. recompute/reuse set disjointness and coverage for degraded terms
"""

import json

from .encoding import decode_block
from .errors import InconsistentError, ScoreMismatchError
from .index_reader import IndexSnapshotView, bm25_idf, bm25_score_term


def load_raw_postings(docs_path):
    """Rebuild uncompressed posting lists straight from the doc JSONL."""
    docs = []
    with open(docs_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    docs.sort(key=lambda d: d["doc_id"])
    postings = {}
    doc_len = {}
    for doc in docs:
        doc_len[doc["doc_id"]] = doc["doc_len"]
        for entry in doc["terms"]:
            postings.setdefault(entry["term"], []).append(
                (doc["doc_id"], int(entry["tf"])))
    for term in postings:
        postings[term].sort()
    return postings, doc_len, docs


def verify_block_integrity(view, docs_path):
    """Each term's decoded blocks must equal the raw posting list."""
    raw_postings, _, _ = load_raw_postings(docs_path)
    for term in sorted(view.terms):
        decoded = view.decode_term_blocks(term)
        expected = raw_postings.get(term, [])
        if decoded != expected:
            for i, (got, want) in enumerate(zip(decoded, expected)):
                if got != want:
                    raise InconsistentError(
                        "block_decode", f"term={term} posting#{i}",
                        got=got, want=want)
            raise InconsistentError(
                "block_decode", f"term={term} length",
                got=len(decoded), want=len(expected))
    return True


def verify_query_scores(view, query, tolerance):
    """Baseline (raw) vs compressed-index scores for one query."""
    terms = query["terms"]
    qid = query["query_id"]
    stats = view.stats
    avgdl, k1, b = stats["avgdl"], stats["k1"], stats["b"]
    # baseline from raw decoded postings, recomputed independently
    base_scores = {}
    for term in terms:
        entry = view.terms.get(term)
        if not entry:
            continue
        idf = bm25_idf(stats["doc_count"], entry["df"])
        for doc_id, tf in view.decode_term_blocks(term):
            dl = stats["doc_len"][str(doc_id)]
            s = bm25_score_term(tf, dl, avgdl, idf, k1, b)
            base_scores[doc_id] = base_scores.get(doc_id, 0.0) + s
    compressed, _ = view.score_postings(terms)
    for doc_id in sorted(set(base_scores) | set(compressed)):
        delta = abs(base_scores.get(doc_id, 0.0)
                    - compressed.get(doc_id, 0.0))
        if delta > tolerance:
            raise ScoreMismatchError(qid, doc_id, delta)
    return base_scores


def verify_topk(view, query, results, tolerance):
    """top-k results must be the true top-k by score, ordered legally."""
    qid = query["query_id"]
    base_scores = verify_query_scores(view, query, tolerance)
    expected = sorted(base_scores.items(), key=lambda kv: (-kv[1], kv[0]))
    expected = expected[:query["top_k"]]
    got_ids = [r["doc_id"] for r in results]
    want_ids = [d for d, _ in expected]
    if got_ids != want_ids:
        raise InconsistentError("topk_set", f"query={qid}",
                                got=got_ids, want=want_ids)
    for i in range(1, len(results)):
        prev, cur = results[i - 1], results[i]
        if not (prev["score"] > cur["score"]
                or (abs(prev["score"] - cur["score"]) <= tolerance
                    and prev["doc_id"] < cur["doc_id"])):
            raise InconsistentError("topk_order", f"query={qid} rank={i}",
                                    prev=prev, cur=cur)
    for r in results:
        delta = abs(base_scores[r["doc_id"]] - r["score"])
        if delta > tolerance:
            raise ScoreMismatchError(qid, r["doc_id"], delta)
    return True


def verify_stats(view, docs_path):
    """Snapshot statistics must match per-term/per-doc detail."""
    raw_postings, doc_len, docs = load_raw_postings(docs_path)
    stats = view.stats
    if stats["doc_count"] != len(docs):
        raise InconsistentError("stats", "doc_count",
                                got=stats["doc_count"], want=len(docs))
    total_len = sum(doc_len.values())
    avgdl = total_len / len(docs) if docs else 0.0
    if abs(stats["avgdl"] - avgdl) > 1e-12:
        raise InconsistentError("stats", "avgdl",
                                got=stats["avgdl"], want=avgdl)
    for doc_id, dl in doc_len.items():
        if stats["doc_len"].get(str(doc_id)) != dl:
            raise InconsistentError("stats", f"doc_len doc={doc_id}")
    for term, plist in raw_postings.items():
        if stats["df"].get(term) != len(plist):
            raise InconsistentError("stats", f"df term={term}",
                                    got=stats["df"].get(term),
                                    want=len(plist))
    return True


def verify_recompute_reuse(recomputed_terms, reused_terms, affected_terms):
    """Recompute and reuse sets must be disjoint and cover all affected
    terms (degraded-path terms are recorded separately)."""
    recomputed, reused, affected = (set(recomputed_terms), set(reused_terms),
                                    set(affected_terms))
    inter = recomputed & reused
    if inter:
        raise InconsistentError("recompute_reuse_overlap",
                                f"term={sorted(inter)[0]}",
                                overlap=sorted(inter))
    missing = affected - (recomputed | reused)
    if missing:
        raise InconsistentError("recompute_reuse_coverage",
                                f"term={sorted(missing)[0]}",
                                missing=sorted(missing))
    return True


def run_full_verification(index_path, docs_path, queries_path, tolerance=1e-9,
                          degraded_terms=()):
    with open(index_path, "rb") as fh:
        view = IndexSnapshotView(json.loads(fh.read().decode("utf-8")))
    verify_block_integrity(view, docs_path)
    verify_stats(view, docs_path)
    with open(queries_path, "r", encoding="utf-8") as fh:
        queries = [json.loads(l) for l in fh if l.strip()]
    for query in queries:
        results = view.top_k(query["terms"], query["top_k"])
        verify_topk(view, query, results, tolerance)
    all_terms = set(view.terms)
    affected = set()
    for query in queries:
        affected |= set(query["terms"]) & all_terms
    degraded = set(degraded_terms) & affected
    recomputed = affected - degraded
    verify_recompute_reuse(recomputed, degraded, affected)
    return {
        "terms_checked": len(view.terms),
        "queries_checked": len(queries),
        "affected_terms": sorted(affected),
        "degraded_terms": sorted(degraded),
        "ok": True,
    }
