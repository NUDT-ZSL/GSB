"""Fixed-seed local sample generator.

The sample is curated rather than random-looking so acceptance runs reliably
cover aliases, cross-sentence links, conflict arbitration and incremental
changes. ``SEED`` is still recorded and used to initialize Python's RNG.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .canonical import write_json, write_jsonl
from .rules import write_v1, write_v2


SEED = 20261127
ORIGIN_MS = 1761955200000


def mention(
    mention_id: str,
    entity_type: str,
    surface: str,
    sentence: str,
    *,
    occurrence: int = 0,
    canonical_name: str | None = None,
) -> dict[str, Any]:
    start = -1
    search_from = 0
    for _ in range(occurrence + 1):
        start = sentence.index(surface, search_from)
        search_from = start + len(surface)
    return {
        "mention_id": mention_id,
        "name": canonical_name or surface,
        "text": surface,
        "entity_type": entity_type,
        "start": start,
        "end": start + len(surface),
    }


def build_sentences() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    text = "Nova Robotics unveiled Atlas Eye at the Shanghai summit."
    rows.append(
        {
            "doc_id": "doc-001",
            "sent_id": 0,
            "sentence": text,
            "mentions": [
                mention("m-0001", "ORG", "Nova Robotics", text),
                mention("m-0002", "PRODUCT", "Atlas Eye", text),
                mention("m-0003", "LOC", "Shanghai", text),
            ],
        }
    )

    text = "Nova said the new Atlas Eye X1 improves warehouse safety."
    rows.append(
        {
            "doc_id": "doc-001",
            "sent_id": 1,
            "sentence": text,
            "mentions": [
                mention("m-0004", "ORG", "Nova", text),
                mention("m-0005", "PRODUCT", "Atlas Eye X1", text),
            ],
        }
    )

    text = "Dr. Lin Wei explained that Atlas Eye X1 will be deployed next year."
    rows.append(
        {
            "doc_id": "doc-001",
            "sent_id": 2,
            "sentence": text,
            "mentions": [
                mention("m-0006", "PER", "Dr. Lin Wei", text),
                mention("m-0007", "PRODUCT", "Atlas Eye X1", text),
            ],
        }
    )

    text = "Lin said Nova expects the Atlas Eye family to help."
    rows.append(
        {
            "doc_id": "doc-001",
            "sent_id": 3,
            "sentence": text,
            "mentions": [
                mention("m-0008", "PER", "Lin", text),
                mention("m-0009", "ORG", "Nova", text),
                mention("m-0010", "PRODUCT", "Atlas Eye", text),
            ],
        }
    )

    text = "Blue Harbor Capital led the infrastructure funding round."
    rows.append(
        {
            "doc_id": "doc-002",
            "sent_id": 0,
            "sentence": text,
            "mentions": [mention("m-0011", "ORG", "Blue Harbor Capital", text)],
        }
    )

    text = "Blue Harbor invested through its infrastructure fund."
    rows.append(
        {
            "doc_id": "doc-002",
            "sent_id": 1,
            "sentence": text,
            "mentions": [mention("m-0012", "ORG", "Blue Harbor", text)],
        }
    )

    text = "Blue Harbor Ventures announced a separate startup accelerator."
    rows.append(
        {
            "doc_id": "doc-002",
            "sent_id": 2,
            "sentence": text,
            "mentions": [mention("m-0013", "ORG", "Blue Harbor Ventures", text)],
        }
    )

    text = "A spokesperson said Blue Harbor will expand the infrastructure fund."
    rows.append(
        {
            "doc_id": "doc-002",
            "sent_id": 3,
            "sentence": text,
            "mentions": [mention("m-0014", "ORG", "Blue Harbor", text)],
        }
    )

    text = "Mingzhou University opened a robotics institute; Professor Chen Rui attended."
    rows.append(
        {
            "doc_id": "doc-003",
            "sent_id": 0,
            "sentence": text,
            "mentions": [
                mention("m-0015", "ORG", "Mingzhou University", text),
                mention("m-0018", "PER", "Professor Chen Rui", text),
            ],
        }
    )

    text = "Mingzhou Univ. robotics institute partnered with city schools."
    rows.append(
        {
            "doc_id": "doc-003",
            "sent_id": 1,
            "sentence": text,
            "mentions": [mention("m-0016", "ORG", "Mingzhou Univ.", text)],
        }
    )

    text = "Mingzhou University said Chen Rui will lead the laboratory."
    rows.append(
        {
            "doc_id": "doc-003",
            "sent_id": 2,
            "sentence": text,
            "mentions": [
                mention("m-0017", "ORG", "Mingzhou University", text),
                mention("m-0019", "PER", "Chen Rui", text),
            ],
        }
    )

    text = "Hangzhou Lake Lab published a deterministic checker."
    rows.append(
        {
            "doc_id": "doc-004",
            "sent_id": 0,
            "sentence": text,
            "mentions": [mention("m-0020", "ORG", "Lake Lab", text)],
        }
    )

    text = "Lake Lab released its cache verifier."
    rows.append(
        {
            "doc_id": "doc-004",
            "sent_id": 1,
            "sentence": text,
            "mentions": [mention("m-0021", "ORG", "Lake Lab", text)],
        }
    )
    return rows


def build_seed_candidates() -> list[dict[str, str]]:
    pairs = [
        ("m-0001", "m-0004", "cross_sentence_seed"),
        ("m-0001", "m-0009", "cross_sentence_seed"),
        ("m-0004", "m-0009", "cross_sentence_seed"),
        ("m-0002", "m-0005", "cross_sentence_seed"),
        ("m-0005", "m-0007", "cross_sentence_seed"),
        ("m-0002", "m-0010", "cross_sentence_seed"),
        ("m-0006", "m-0008", "cross_sentence_seed"),
        ("m-0011", "m-0012", "cross_sentence_seed"),
        ("m-0011", "m-0014", "cross_sentence_seed"),
        ("m-0012", "m-0014", "cross_sentence_seed"),
        ("m-0013", "m-0014", "ambiguity_seed"),
        ("m-0015", "m-0016", "cross_sentence_seed"),
        ("m-0015", "m-0017", "cross_sentence_seed"),
        ("m-0016", "m-0017", "cross_sentence_seed"),
        ("m-0018", "m-0019", "cross_sentence_seed"),
        ("m-0020", "m-0021", "cross_sentence_seed"),
    ]
    return [
        {"pair_key": f"{left}|{right}", "mention_a": left, "mention_b": right, "origin": origin}
        for left, right, origin in pairs
    ]


def build_rule_book() -> dict[str, Any]:
    weights = {
        "shared_proportion": 4_500,
        "distance_decay": 2_500,
        "document_scale": 1_500,
        "rule_algebra": 1_500,
    }
    specs = [
        ("ctx-org", "ORG", 3, 230, 5_800, 3_200),
        ("ctx-per", "PER", 2, 190, 5_300, 3_000),
        ("ctx-product", "PRODUCT", 3, 210, 6_000, 3_100),
        ("ctx-loc", "LOC", 2, 180, 6_500, 2_800),
    ]
    rules: list[dict[str, Any]] = [
        {
            "rule_id": "ctx-org-v1-deprecated",
            "family": "context",
            "generation": 1,
            "algebra_generation": 1,
            "entity_type": "ORG",
            "sentence_window": 2,
            "position_scale_cp": 180,
            "weights_bps": weights,
            "threshold_bps": 7_200,
            "algebra_bias_raw_bps": 1_800,
            "effective_at_ms": ORIGIN_MS,
            "tombstone": True,
            "description": {"text": "deprecated strict organization context rule"},
        }
    ]
    for rule_id, entity_type, window, scale, threshold, bias in specs:
        rules.append(
            {
                "rule_id": rule_id,
                "family": "context",
                "generation": 2,
                "algebra_generation": 2,
                "entity_type": entity_type,
                "sentence_window": window,
                "position_scale_cp": scale,
                "weights_bps": weights,
                "threshold_bps": threshold,
                "algebra_bias_raw_bps": bias,
                "effective_at_ms": ORIGIN_MS + 3_600_000,
                "tombstone": False,
                "description": {
                    "text": f"deterministic {entity_type} type/position/window context rule",
                    "features": ["entity_type", "codepoint_position", "sentence_window"],
                },
            }
        )
    return {
        "internal_schema": "entity-link-rules-internal/v1",
        "snapshot_id": "ner-link-rules-seed",
        "origin_effective_at_ms": ORIGIN_MS,
        "rules": rules,
        "generation_timeline_ms": [
            {"generation": 1, "effective_at_ms": ORIGIN_MS},
            {"generation": 2, "effective_at_ms": ORIGIN_MS + 3_600_000},
        ],
    }


def build_delta() -> dict[str, Any]:
    new_text = "Mingzhou University opened an open reproduction repository."
    added_row = {
        "doc_id": "doc-003",
        "sent_id": 3,
        "sentence": new_text,
        "mentions": [mention("m-0022", "ORG", "Mingzhou University", new_text)],
    }
    revised_text = "Hangzhou Lake Lab published its deterministic checker update."
    revised_row = {
        "doc_id": "doc-004",
        "sent_id": 1,
        "sentence": revised_text,
        "mentions": [
            mention(
                "m-0021",
                "ORG",
                "Hangzhou Lake Lab",
                revised_text,
                canonical_name="Hangzhou Lake Lab",
            )
        ],
        "revision_reason": "canonical mention expanded from Lake Lab to Hangzhou Lake Lab",
    }
    return {
        "delta_id": "delta-after-sentence-3",
        "add_sentences": [added_row],
        "revise_sentences": [revised_row],
    }


def generate_all(out_dir: str | Path = "data") -> dict[str, Any]:
    rng = random.Random(SEED)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = build_sentences()
    rng.shuffle(rows)
    rows.sort(key=lambda row: (row["doc_id"], row["sent_id"]))
    write_jsonl(out / "mentions.jsonl", rows)
    candidates = build_seed_candidates()
    write_jsonl(out / "coref_candidates.jsonl", candidates)

    book = build_rule_book()
    v2_dir = out / "rules_v2"
    write_v2(book, v2_dir)
    write_v1(book, out / "rules_v1.json", mode="fold")
    delta = build_delta()
    write_json(out / "incremental_delta.json", delta)

    manifest = {
        "seed": SEED,
        "generator": "entity_link_lab.seed",
        "mention_rows": len(rows),
        "mention_count": sum(len(row["mentions"]) for row in rows),
        "seed_candidate_count": len(candidates),
        "files": {
            "mentions": "mentions.jsonl",
            "candidates": "coref_candidates.jsonl",
            "rules_v1": "rules_v1.json",
            "rules_v2_manifest": "rules_v2/manifest.json",
            "rules_v2_sqlite": "rules_v2/rules_meta.db",
            "delta": "incremental_delta.json",
        },
        "determinism_note": "random.Random(seed) is initialized and used before stable sorting",
    }
    write_json(out / "seed_manifest.json", manifest)
    return manifest
