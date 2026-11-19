"""Deterministic local sample generator (fixed seed, no network).

Generates:
  pairs.jsonl          multilingual query-document pairs
  lang_pairs.json      versioned language-pair map resource
  translations.json    versioned word-level translation table resource
  weights.json         versioned scoring weights resource
  config.json          pipeline configuration

The generator deliberately includes untranslated concepts, a duplicate
conflicting translation entry and out-of-map language pairs so the
fallback / conflict / degradation paths are exercised by the demo data.
"""

import json
import os
import random

from .resources import (
    KIND_LANG_PAIRS,
    KIND_TRANSLATIONS,
    KIND_WEIGHTS,
    build_envelope,
)
from .util import canonical_dumps

# concept_id -> (en, es, de, fr, zh)
CONCEPTS = [
    ("climate", "clima", "klima", "climat", "气候"),
    ("ocean", "océano", "ozean", "océan", "海洋"),
    ("energy", "energía", "energie", "énergie", "能源"),
    ("solar", "solar", "solar", "solaire", "太阳能"),
    ("wind", "viento", "wind", "vent", "风"),
    ("forest", "bosque", "wald", "forêt", "森林"),
    ("river", "río", "fluss", "rivière", "河流"),
    ("city", "ciudad", "stadt", "ville", "城市"),
    ("transport", "transporte", "transport", "transport", "交通"),
    ("health", "salud", "gesundheit", "santé", "健康"),
    ("economy", "economía", "wirtschaft", "économie", "经济"),
    ("market", "mercado", "markt", "marché", "市场"),
    ("policy", "política", "politik", "politique", "政策"),
    ("water", "agua", "wasser", "eau", "水"),
    ("food", "comida", "essen", "nourriture", "食物"),
    ("agriculture", "agricultura", "landwirtschaft", "agriculture", "农业"),
    ("technology", "tecnología", "technologie", "technologie", "技术"),
    ("data", "datos", "daten", "données", "数据"),
    ("network", "red", "netzwerk", "réseau", "网络"),
    ("security", "seguridad", "sicherheit", "sécurité", "安全"),
    ("education", "educación", "bildung", "éducation", "教育"),
    ("science", "ciencia", "wissenschaft", "science", "科学"),
    ("research", "investigación", "forschung", "recherche", "研究"),
    ("medicine", "medicina", "medizin", "médecine", "医学"),
    ("virus", "virus", "virus", "virus", "病毒"),
    ("vaccine", "vacuna", "impfstoff", "vaccin", "疫苗"),
    ("trade", "comercio", "handel", "commerce", "贸易"),
    ("growth", "crecimiento", "wachstum", "croissance", "增长"),
    ("inflation", "inflación", "inflation", "inflation", "通胀"),
    ("employment", "empleo", "beschäftigung", "emploi", "就业"),
    ("housing", "vivienda", "wohnen", "logement", "住房"),
    ("poverty", "pobreza", "armut", "pauvreté", "贫困"),
    ("migration", "migración", "migration", "migration", "迁移"),
    ("border", "frontera", "grenze", "frontière", "边境"),
    ("treaty", "tratado", "vertrag", "traité", "条约"),
    ("election", "elección", "wahl", "élection", "选举"),
]

LANG_INDEX = {"en": 0, "es": 1, "de": 2, "fr": 3, "zh": 4}
LANGUAGES = ["en", "es", "de", "fr", "zh"]

# allowed directed language pairs (zh<->es deliberately absent -> no_langpair)
LANG_PAIRS = [
    ("en", "es"), ("es", "en"),
    ("en", "de"), ("de", "en"),
    ("en", "fr"), ("fr", "en"),
    ("en", "zh"), ("zh", "en"),
    ("es", "de"), ("de", "es"),
]

# concepts whose translation is missing for a pair direction (fallback path)
def _is_missing(concept_id, src, dst):
    return (concept_id * 7 + LANG_INDEX[src] * 3 + LANG_INDEX[dst]) % 11 == 0


def build_lang_pairs_payload():
    return {"pairs": [{"src": s, "dst": d} for s, d in LANG_PAIRS]}


def build_translations_payload():
    entries = []
    for src, dst in LANG_PAIRS:
        si, di = LANG_INDEX[src], LANG_INDEX[dst]
        for cid, concept in enumerate(CONCEPTS):
            if _is_missing(cid, src, dst):
                continue
            entries.append({
                "src": src, "dst": dst,
                "term": concept[si],
                "candidates": [concept[di]],
            })
    # deliberate conflicting duplicate: first entry wins, conflict is logged
    entries.append({"src": "en", "dst": "es", "term": "data",
                    "candidates": ["información"]})
    # multi-candidate entry to exercise the candidate limit degradation
    for entry in entries:
        if (entry["src"], entry["dst"], entry["term"]) == ("en", "de", "network"):
            entry["candidates"] = ["netzwerk", "datennetz", "verbundnetz",
                                   "rechnernetz"]
            break
    return {"entries": entries}


def build_weights_payload():
    return {
        "w_tfidf": 600000,
        "w_coverage": 400000,
        "grade_thresholds": {"1": 120000, "2": 380000, "3": 680000},
    }


_FILLERS = {
    "en": ["the", "and", "of", "in"],
    "es": ["el", "la", "y", "de"],
    "de": ["der", "die", "und", "das"],
    "fr": ["le", "la", "et", "les"],
    "zh": [],
}


def _text(rng, lang, concept_ids):
    words = [CONCEPTS[c][LANG_INDEX[lang]] for c in concept_ids]
    fillers = _FILLERS[lang]
    out = []
    for i, word in enumerate(words):
        out.append(word)
        if fillers and i < len(words) - 1:
            out.append(fillers[rng.randrange(len(fillers))])
    return " ".join(out)


def gen_pairs(n_pairs, seed):
    rng = random.Random(seed)
    n_concepts = len(CONCEPTS)
    pairs = []
    usable = [(s, d) for s, d in LANG_PAIRS if s != d]
    for i in range(n_pairs):
        # ~15% of pairs use an out-of-map direction to hit no_langpair
        if rng.random() < 0.15:
            q_lang, d_lang = "zh", "es" if rng.random() < 0.5 else "fr"
        else:
            q_lang, d_lang = usable[rng.randrange(len(usable))]
        n_query = rng.randint(3, 6)
        query_concepts = rng.sample(range(n_concepts), n_query)
        overlap = rng.randint(0, n_query)
        shared = query_concepts[:overlap]
        distractors = rng.sample(
            [c for c in range(n_concepts) if c not in query_concepts],
            rng.randint(2, 8))
        doc_concepts = shared + distractors
        rng.shuffle(doc_concepts)
        frac = overlap / n_query
        noise = rng.choice((-1, 0, 0, 1))
        grade = max(0, min(3, round(frac * 3) + (noise if rng.random() < 0.3 else 0)))
        pairs.append({
            "pair_id": f"p{i:04d}",
            "query_id": f"q{i % max(1, n_pairs // 3):04d}",
            "query_lang": q_lang,
            "doc_lang": d_lang,
            "query_text": _text(rng, q_lang, query_concepts),
            "doc_text": _text(rng, d_lang, doc_concepts),
            "relevance_grade": grade,
        })
    return pairs


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(canonical_dumps(obj))
        fh.write("\n")


def generate(out_dir, resources_dir, n_pairs=120, seed=20261120,
             resource_version=1):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(resources_dir, exist_ok=True)

    pairs = gen_pairs(n_pairs, seed)
    with open(os.path.join(out_dir, "pairs.jsonl"), "w", encoding="utf-8") as fh:
        for pair in pairs:
            fh.write(canonical_dumps(pair))
            fh.write("\n")

    _write_json(os.path.join(resources_dir, "lang_pairs.json"),
                build_envelope(KIND_LANG_PAIRS, resource_version,
                               build_lang_pairs_payload()))
    _write_json(os.path.join(resources_dir, "translations.json"),
                build_envelope(KIND_TRANSLATIONS, resource_version,
                               build_translations_payload()))
    _write_json(os.path.join(resources_dir, "weights.json"),
                build_envelope(KIND_WEIGHTS, resource_version,
                               build_weights_payload()))

    config = {
        "languages": LANGUAGES,
        "fallback_strategy": "keep_source",
        "limits": {
            "max_pairs_per_stage": 100000,
            "max_alignment_candidates": 3,
            "max_working_bytes": 33554432,
        },
    }
    _write_json(os.path.join(out_dir, "config.json"), config)
    return {"pairs": len(pairs), "seed": seed,
            "resource_version": resource_version}
