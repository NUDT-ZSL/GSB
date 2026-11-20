"""Deterministic local sample generation.

Generates multilingual document/query JSONL plus three versions of the
bridge resource (v1 -> v2 touches only en->de entries, v2 -> v3 touches
en->fr and de->en) so incremental partition rebuilds and recovery can be
exercised without any online service or real corpus.
"""

import json
from pathlib import Path

import numpy as np

from .bridge import build_table, build_weights
from .canon import canonical_text, checksum

SEED = 20261121
LANGS = ["de", "en", "fr", "zh"]
DIRECTIONS = [("en", "de"), ("en", "fr"), ("en", "zh"),
              ("de", "en"), ("fr", "en")]
N_CONCEPTS = 60
N_FILLER_PER_LANG = 40
DOCS_PER_LANG = 60
N_QUERIES = 24


def _concept_term(ci, lang):
    return f"c{ci:04d}_{lang}"


def _filler_term(fi, lang):
    return f"{lang}_w{fi:04d}"


def _conf(rng, lo=0.35, hi=0.98):
    return round(float(rng.uniform(lo, hi)), 4)


def generate_documents(path, seed=SEED):
    rng = np.random.default_rng(seed)
    rows = []
    doc_no = 0
    for lang in LANGS:
        for _ in range(DOCS_PER_LANG):
            doc_no += 1
            n_concepts = int(rng.integers(3, 9))
            n_fillers = int(rng.integers(2, 7))
            terms = {}
            for ci in rng.choice(N_CONCEPTS, size=n_concepts, replace=False):
                terms[_concept_term(int(ci), lang)] = int(rng.integers(1, 4))
            for fi in rng.choice(N_FILLER_PER_LANG, size=n_fillers,
                                 replace=False):
                terms[_filler_term(int(fi), lang)] = int(rng.integers(1, 3))
            term_list = [{"term": t, "tf": terms[t]} for t in sorted(terms)]
            rows.append({
                "doc_id": f"d{doc_no:04d}",
                "doc_lang": lang,
                "terms": term_list,
                "doc_len": sum(t["tf"] for t in term_list),
            })
    _write_jsonl(path, rows)
    return rows


def generate_queries(path, seed=SEED):
    rng = np.random.default_rng(seed + 7)
    rows = []
    query_langs = ["en"] * 14 + ["de"] * 5 + ["fr"] * 5
    for qi in range(N_QUERIES):
        lang = query_langs[qi]
        n_terms = int(rng.integers(1, 4))
        terms = []
        for ci in rng.choice(N_CONCEPTS, size=n_terms, replace=False):
            terms.append({"term": _concept_term(int(ci), lang), "tf": 1})
        if qi % 5 == 4:
            terms.append({"term": _filler_term(int(rng.integers(0, 10)), lang),
                          "tf": 1})
        rows.append({
            "query_id": f"q{qi + 1:04d}",
            "query_lang": lang,
            "terms": terms,
            "top_k": int(rng.integers(1, 11)),
        })
    _write_jsonl(path, rows)
    return rows


def _direction_entries():
    return [{"src_lang": s, "tgt_lang": t, "enabled": True}
            for s, t in DIRECTIONS]


def _term_entries(version, seed=SEED):
    """Term-bridge entries. v2 rewrites en->de, v3 rewrites en->fr, de->en.

    Each entry draws from its own RNG stream keyed by (direction, concept)
    so untouched language pairs stay byte-identical across versions, and
    version-specific modifications draw from a stream keyed by version.
    """
    entries = []
    for dir_idx, (src, tgt) in enumerate(DIRECTIONS):
        for ci in range(N_CONCEPTS):
            entry_rng = np.random.default_rng(seed + dir_idx * 100003 + ci)
            src_term = _concept_term(ci, src)
            candidates = [{"term": _concept_term(ci, tgt),
                           "confidence": _conf(entry_rng, 0.75, 0.98)}]
            for _ in range(int(entry_rng.integers(0, 3))):
                near = int(entry_rng.integers(0, N_CONCEPTS))
                candidates.append({"term": _concept_term(near, tgt),
                                   "confidence": _conf(entry_rng, 0.35, 0.7)})
            if version >= 2 and (src, tgt) == ("en", "de") and ci % 3 == 0:
                mod_rng = np.random.default_rng(
                    seed + 900_000_000 + 2 * 10007 + dir_idx * 101 + ci)
                candidates.append({"term": _filler_term(ci % 5, tgt),
                                   "confidence": _conf(mod_rng, 0.4, 0.6)})
            if version >= 3 and (src, tgt) in (("en", "fr"), ("de", "en")) \
                    and ci % 4 == 0:
                mod_rng = np.random.default_rng(
                    seed + 900_000_000 + 3 * 10007 + dir_idx * 101 + ci)
                candidates = [c for c in candidates
                              if c["confidence"] > 0.5] or candidates[:1]
                candidates.append({"term": _filler_term(ci % 7, tgt),
                                   "confidence": _conf(mod_rng, 0.45, 0.65)})
            candidates.sort(key=lambda c: (-c["confidence"], c["term"]))
            entries.append({
                "src_lang": src,
                "src_term": src_term,
                "tgt_lang": tgt,
                "candidates": candidates,
            })
    entries.sort(key=lambda e: (e["src_lang"], e["src_term"], e["tgt_lang"]))
    return entries


def generate_bridge(path, version, seed=SEED):
    resource = {
        "resource_id": "xbridge",
        "resource_version": version,
        "languages": LANGS,
        "direction_table": build_table(version, _direction_entries()),
        "term_bridge": build_table(version, _term_entries(version, seed)),
        "scoring_weights": build_weights(version, {
            "k1": 1.2,
            "b": 0.75,
            "combine_rule": "weighted_sum",
            "max_candidates_per_term": 3,
            "min_confidence": 0.05,
            "max_expansion_candidates": 512,
            "max_postings_per_partition": 100000,
            "max_scoring_entries": 200000,
            "resource_policy": "degrade",
        }),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(canonical_text(resource), encoding="utf-8")
    return resource


def generate_bad_samples(demo_dir):
    """Invalid inputs exercising the business error codes."""
    demo = Path(demo_dir)
    demo.mkdir(parents=True, exist_ok=True)

    _write_jsonl(demo / "bad_duplicate_docs.jsonl", [
        {"doc_id": "d9001", "doc_lang": "en",
         "terms": [{"term": "c0001_en", "tf": 1}], "doc_len": 1},
        {"doc_id": "d9001", "doc_lang": "en",
         "terms": [{"term": "c0002_en", "tf": 1}], "doc_len": 1},
    ])
    _write_jsonl(demo / "bad_empty_term.jsonl", [
        {"doc_id": "d9002", "doc_lang": "en",
         "terms": [{"term": "", "tf": 1}], "doc_len": 1},
    ])
    _write_jsonl(demo / "bad_top_k.jsonl", [
        {"query_id": "q9001", "query_lang": "en",
         "terms": [{"term": "c0001_en", "tf": 1}], "top_k": 0},
    ])
    (demo / "malformed.jsonl").write_text(
        '{"doc_id": "d9003", "doc_lang": "en", "terms": [}\n', encoding="utf-8")

    base = generate_bridge(demo / "bad_confidence.json", 1)
    base["term_bridge"]["entries"][0]["candidates"][0]["confidence"] = 1.5
    base["term_bridge"] = build_table(1, base["term_bridge"]["entries"])
    (demo / "bad_confidence.json").write_text(canonical_text(base),
                                              encoding="utf-8")

    base = generate_bridge(demo / "bad_bridge_lang.json", 1)
    base["term_bridge"]["entries"][0]["tgt_lang"] = "xx"
    base["term_bridge"] = build_table(1, base["term_bridge"]["entries"])
    (demo / "bad_bridge_lang.json").write_text(canonical_text(base),
                                               encoding="utf-8")

    base = generate_bridge(demo / "bad_weights.json", 1)
    base["scoring_weights"]["params"]["combine_rule"] = "magic_merge"
    base["scoring_weights"]["checksum"] = checksum(
        base["scoring_weights"]["params"])
    (demo / "bad_weights.json").write_text(canonical_text(base),
                                           encoding="utf-8")


def generate_all(data_dir, seed=SEED):
    data = Path(data_dir)
    data.mkdir(parents=True, exist_ok=True)
    generate_documents(data / "documents.jsonl", seed)
    generate_queries(data / "queries.jsonl", seed)
    for version in (1, 2, 3):
        generate_bridge(data / f"bridge_v{version}.json", version, seed)
    meta = {"seed": seed, "langs": LANGS, "docs_per_lang": DOCS_PER_LANG,
            "n_queries": N_QUERIES, "bridge_versions": [1, 2, 3]}
    (data / "sample_meta.json").write_text(canonical_text(meta),
                                           encoding="utf-8")
    return meta


def _write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
