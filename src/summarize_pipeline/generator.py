from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .config import parse_config
from .textproc import split_sentence_text, tokenize
from .utils import write_json_atomic, write_jsonl_atomic


VOCABULARY = (
    "anchor",
    "baseline",
    "bridge",
    "buffer",
    "catalog",
    "channel",
    "cluster",
    "compute",
    "context",
    "delta",
    "domain",
    "drift",
    "engine",
    "estimate",
    "feature",
    "filter",
    "formal",
    "gradient",
    "graph",
    "index",
    "kernel",
    "latency",
    "ledger",
    "margin",
    "matrix",
    "metric",
    "model",
    "normal",
    "offset",
    "packet",
    "pipeline",
    "policy",
    "quantize",
    "range",
    "reference",
    "region",
    "sample",
    "scale",
    "segment",
    "signal",
    "stable",
    "stream",
    "summary",
    "target",
    "tensor",
    "threshold",
    "token",
    "trace",
    "vector",
    "window",
)


def _make_sentence(rng: random.Random, anchors: list[str]) -> str:
    length = rng.randint(7, 13)
    words = [rng.choice(VOCABULARY) for _ in range(length)]
    for anchor in anchors:
        words[rng.randrange(length)] = anchor
    return " ".join(words) + "."


def generate_rows(
    *,
    seed: int = 20261121,
    doc_count: int = 12,
    vocabulary: tuple[str, ...] = VOCABULARY,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    for doc_index in range(doc_count):
        doc_id = f"doc-{doc_index + 1:04d}"
        ref_units = tuple(sorted(rng.sample(vocabulary, 6)))
        sentence_count = rng.randint(9, 14)
        anchors_per_sentence: list[list[str]] = [[] for _ in range(sentence_count)]
        for unit_index, unit in enumerate(ref_units):
            anchor_positions = sorted(
                rng.sample(range(sentence_count), k=rng.randint(2, 3))
            )
            for position in anchor_positions:
                if unit not in anchors_per_sentence[position]:
                    anchors_per_sentence[position].append(unit)

        sentence_texts = [
            _make_sentence(rng, anchors_per_sentence[index])
            for index in range(sentence_count)
        ]
        full_text = " ".join(sentence_texts)
        split_texts = split_sentence_text(full_text)
        if len(split_texts) != sentence_count:
            raise RuntimeError("internal sentence split mismatch")
        for sent_no, text in enumerate(split_texts):
            if not tokenize(text):
                raise RuntimeError("generated an empty token sentence")
            rows.append(
                {
                    "doc_id": doc_id,
                    "sent_no": sent_no,
                    "text": text,
                    "char_len": len(text),
                    "ref_summary_units": list(ref_units),
                }
            )
    return rows


def default_config_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "controlled_vocab": list(VOCABULARY),
        "weights": {
            "position": 0.30,
            "length": 0.20,
            "frequency": 0.30,
            "coverage": 0.20,
        },
        "sentence_threshold": 0.40,
        "rewrite_threshold": 0.01,
        "aggregate_threshold": 0.35,
        "length_budget": 420,
        "compression_ratio": 0.70,
        "temperature": 0.85,
        "smoothing": 0.05,
        "precision": {
            "mode": "float64",
            "scale_factor": 1_000_000,
            "rounding": "nearest_even",
        },
        "threshold_policy": "tolerance",
        "tolerance_abs": 1e-6,
        "subnormal_floor": 1e-12,
        "ideal_sentence_chars": 72,
        "output_precision": 12,
    }


def generate_bundle(
    data_path: str | Path,
    config_path: str | Path,
    *,
    seed: int = 20261121,
    doc_count: int = 12,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = generate_rows(seed=seed, doc_count=doc_count)
    config_payload = default_config_payload()
    parse_config(config_payload)
    write_jsonl_atomic(data_path, rows)
    write_json_atomic(config_path, config_payload, pretty=True)
    return rows, config_payload

