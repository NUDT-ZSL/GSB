"""The four deterministic stage transformations.

All relevance arithmetic is done in fixed-point micro-units (1e6) with
integer math only, so grade boundaries can never flip on float rounding
and reruns from the same snapshot are byte-identical.
"""

import os

import numpy as np

from .config import FALLBACK_DROP
from .errors import (
    E_DUP_PAIR,
    E_GRADE_INVALID,
    E_LANG_UNSUPPORTED,
    PipelineError,
)
from .textnorm import detect_language, normalize_text
from .util import MICRO, canonical_bytes

STAGE_NORMALIZE = "normalize"
STAGE_ALIGN = "align"
STAGE_SCORE = "score"
STAGE_SUMMARIZE = "summarize"
STAGE_ORDER = (STAGE_NORMALIZE, STAGE_ALIGN, STAGE_SCORE, STAGE_SUMMARIZE)

VIA_DIRECT = "direct"
VIA_KEEP_SOURCE = "keep_source"
VIA_DROPPED = "dropped"
VIA_NO_LANGPAIR = "no_langpair"
FALLBACK_VIAS = (VIA_KEEP_SOURCE, VIA_DROPPED, VIA_NO_LANGPAIR)

DEGRADE_CAND_TRUNC = "cand_trunc"
DEGRADE_MEM_SPILL = "mem_spill"

GRADE_MIN = 0
GRADE_MAX = 3


class WorkingBuffer:
    """Accumulates canonical output records; spills to the working dir when
    the configured resident-memory limit is exceeded (degradation mem_spill).
    """

    def __init__(self, working_path, max_bytes):
        self.working_path = working_path
        self.max_bytes = max_bytes
        self._chunks = []
        self._size = 0
        self._fh = None
        self.spilled = False

    def append(self, record):
        line = canonical_bytes(record) + b"\n"
        if self._fh is not None:
            self._fh.write(line)
            return
        self._chunks.append(line)
        self._size += len(line)
        if self._size > self.max_bytes:
            self._fh = open(self.working_path, "wb")
            for chunk in self._chunks:
                self._fh.write(chunk)
            self._chunks = []
            self.spilled = True

    def finalize(self):
        if self._fh is not None:
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._fh.close()
            with open(self.working_path, "rb") as fh:
                return fh.read()
        return b"".join(self._chunks)


# ---------------- stage 1: normalize ----------------

def validate_pair(raw, cfg):
    pair_id = raw.get("pair_id")
    required = ("pair_id", "query_id", "query_lang", "doc_lang",
                "query_text", "doc_text", "relevance_grade")
    for field in required:
        if field not in raw:
            raise PipelineError(
                "E_INPUT_PARSE", f"pair missing field {field}",
                stage=STAGE_NORMALIZE, pair_id=pair_id)
    for field in ("query_lang", "doc_lang"):
        if raw[field] not in cfg.languages:
            raise PipelineError(
                E_LANG_UNSUPPORTED,
                f"language {raw[field]!r} not in controlled set",
                stage=STAGE_NORMALIZE, pair_id=pair_id)
    grade = raw.get("relevance_grade")
    bad_type = not isinstance(grade, int) or isinstance(grade, bool)
    if bad_type or not (GRADE_MIN <= grade <= GRADE_MAX):
        raise PipelineError(
            E_GRADE_INVALID,
            f"relevance_grade {grade!r} outside [{GRADE_MIN}, {GRADE_MAX}]",
            stage=STAGE_NORMALIZE, pair_id=pair_id)


def normalize_pair(raw, cfg):
    validate_pair(raw, cfg)
    query_terms = normalize_text(raw["query_text"])
    doc_terms = normalize_text(raw["doc_text"])
    q_det = detect_language(query_terms, raw["query_text"], cfg.languages)
    d_det = detect_language(doc_terms, raw["doc_text"], cfg.languages)
    return {
        "pair_id": raw["pair_id"],
        "query_id": raw["query_id"],
        "query_lang": raw["query_lang"],
        "doc_lang": raw["doc_lang"],
        "relevance_grade": raw["relevance_grade"],
        "query_terms": query_terms,
        "doc_terms": doc_terms,
        "query_lang_detected": q_det,
        "doc_lang_detected": d_det,
        "query_lang_match": q_det == raw["query_lang"],
        "doc_lang_match": d_det == raw["doc_lang"],
    }


def stage_normalize(pairs, cfg):
    seen = set()
    for raw in pairs:
        pair_id = raw.get("pair_id")
        if pair_id in seen:
            raise PipelineError(
                E_DUP_PAIR, f"duplicate pair_id {pair_id}",
                stage=STAGE_NORMALIZE, pair_id=pair_id)
        seen.add(pair_id)
        yield normalize_pair(raw, cfg)


# ---------------- stage 2: align ----------------

def align_pair(norm_rec, bundle, cfg):
    q_lang = norm_rec["query_lang"]
    d_lang = norm_rec["doc_lang"]
    limit = cfg.limits.max_alignment_candidates
    aligned = []
    stats = {VIA_DIRECT: 0, VIA_KEEP_SOURCE: 0, VIA_DROPPED: 0,
             VIA_NO_LANGPAIR: 0, DEGRADE_CAND_TRUNC: 0}
    pair_key = f"{q_lang}|{d_lang}"
    if q_lang == d_lang:
        for term in norm_rec["query_terms"]:
            aligned.append({"term": term, "src": term, "via": VIA_DIRECT})
            stats[VIA_DIRECT] += 1
    elif pair_key not in bundle.lang_pairs:
        for term in norm_rec["query_terms"]:
            aligned.append({"term": term, "src": term, "via": VIA_NO_LANGPAIR})
            stats[VIA_NO_LANGPAIR] += 1
    else:
        for term in norm_rec["query_terms"]:
            cands = bundle.translations.get((q_lang, d_lang, term))
            if cands:
                trunc = len(cands) > limit
                kept = cands[:limit]
                if trunc:
                    stats[DEGRADE_CAND_TRUNC] += 1
                aligned.append({
                    "term": kept[0], "src": term, "via": VIA_DIRECT,
                    "alts": kept[1:], "trunc": trunc,
                })
                stats[VIA_DIRECT] += 1
            elif cfg.fallback_strategy == FALLBACK_DROP:
                aligned.append({"term": term, "src": term, "via": VIA_DROPPED})
                stats[VIA_DROPPED] += 1
            else:
                aligned.append({"term": term, "src": term,
                                "via": VIA_KEEP_SOURCE})
                stats[VIA_KEEP_SOURCE] += 1
    return {
        "pair_id": norm_rec["pair_id"],
        "query_lang": q_lang,
        "doc_lang": d_lang,
        "relevance_grade": norm_rec["relevance_grade"],
        "doc_terms": norm_rec["doc_terms"],
        "aligned": aligned,
        "stats": stats,
    }


def stage_align(norm_records, bundle, cfg):
    for rec in norm_records:
        yield align_pair(rec, bundle, cfg)


# ---------------- stage 3: score ----------------

def build_idf(align_records):
    """Document frequency over doc-side terms; idf quantized to micro-units."""
    records = list(align_records)
    n_docs = len(records)
    df = {}
    for rec in records:
        for term in set(rec["doc_terms"]):
            df[term] = df.get(term, 0) + 1
    terms = sorted(df)
    counts = np.array([df[t] for t in terms], dtype=np.float64)
    idf = np.log((n_docs + 1.0) / (counts + 1.0)) + 1.0
    idf_q = {t: int(v) for t, v in zip(terms, np.rint(idf * MICRO))}
    return records, idf_q


def score_pair(align_rec, idf_q, weights):
    aligned_terms = sorted({
        item["term"] for item in align_rec["aligned"]
        if item["via"] != VIA_DROPPED
    })
    doc_set = set(align_rec["doc_terms"])
    matched = [t for t in aligned_terms if t in doc_set]
    num = sum(idf_q.get(t, 0) for t in matched)
    den = sum(idf_q.get(t, 0) for t in aligned_terms)
    tfidf_q = (2 * num * MICRO + den) // (2 * den) if den else 0
    if aligned_terms:
        coverage_q = (2 * len(matched) * MICRO
                      + len(aligned_terms)) // (2 * len(aligned_terms))
    else:
        coverage_q = 0
    score = (weights["w_tfidf"] * tfidf_q
             + weights["w_coverage"] * coverage_q) // MICRO
    return {
        "pair_id": align_rec["pair_id"],
        "relevance_grade": align_rec["relevance_grade"],
        "score": score,
        "tfidf": tfidf_q,
        "coverage": coverage_q,
        "matched": len(matched),
        "aligned": len(aligned_terms),
    }


def stage_score(align_records, weights):
    records, idf_q = build_idf(align_records)
    for rec in records:
        yield score_pair(rec, idf_q, weights)


# ---------------- stage 4: summarize ----------------

def grade_for_score(score, thresholds):
    grade = GRADE_MIN
    for g in range(GRADE_MIN + 1, GRADE_MAX + 1):
        if score >= thresholds.get(str(g), 0):
            grade = g
    return grade


def stage_summarize(score_records, weights):
    thresholds = weights["grade_thresholds"]
    graded = []
    for rec in score_records:
        graded.append({
            "pair_id": rec["pair_id"],
            "score": rec["score"],
            "grade": grade_for_score(rec["score"], thresholds),
            "relevance_grade": rec["relevance_grade"],
        })
    graded.sort(key=lambda r: (-r["score"], r["pair_id"]))
    for rank, rec in enumerate(graded, start=1):
        rec_out = {"rank": rank}
        rec_out.update(rec)
        yield rec_out
