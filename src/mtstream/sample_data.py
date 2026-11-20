"""Fully local deterministic bilingual sample generation."""

from __future__ import annotations

import random
from typing import Any

from .models import Alignment, TranslationCase, finite_float
from .util import qfloat

SOURCE_WORDS: tuple[str, ...] = tuple(f"src_{i:02d}" for i in range(32))
TARGET_WORDS: tuple[str, ...] = tuple(f"tgt_{i:02d}" for i in range(24))


def make_sample_case(
    seed: int = 20261120,
    *,
    case_id: str | None = None,
    source_length: int = 14,
    target_length: int = 11,
) -> TranslationCase:
    """Build a repeatable sentence and its word-level alignment table.

    The chosen source positions intentionally leave at least one source word
    unreferenced, which makes the dependency-aware cache test observable.
    """

    if source_length < 2:
        source_length = 2
    if target_length <= 0:
        target_length = 1
    if target_length > source_length:
        target_length = source_length

    rng = random.Random(seed)
    source_tokens = tuple(
        SOURCE_WORDS[rng.randrange(len(SOURCE_WORDS))]
        for _ in range(source_length)
    )
    referenced = sorted(rng.sample(range(source_length), target_length))
    alignments: list[Alignment] = []
    for target_index, source_index in enumerate(referenced):
        target_token = TARGET_WORDS[
            (target_index * 7 + (seed % len(TARGET_WORDS))) % len(TARGET_WORDS)
        ]
        weight = qfloat(0.65 + 0.70 * rng.random())
        alignments.append(
            Alignment(
                source_index=source_index,
                target_index=target_index,
                target_token=target_token,
                weight=weight,
            )
        )

    pairs = sorted(
        {
            (source_tokens[alignment.source_index], alignment.target_token)
            for alignment in alignments
        }
    )
    return TranslationCase(
        case_id=case_id or f"local-seed-{seed}",
        seed=int(seed),
        source_tokens=source_tokens,
        source_vocab=SOURCE_WORDS,
        target_vocab=TARGET_WORDS,
        pair_vocab=tuple(pairs),
        alignments=tuple(alignments),
    )


def unaligned_source_indices(case: TranslationCase) -> tuple[int, ...]:
    aligned = case.aligned_source_indices()
    return tuple(i for i in range(len(case.source_tokens)) if i not in aligned)


def modification_token_for(case: TranslationCase, index: int) -> str:
    """Pick a vocabulary token that differs from the current sentence token."""

    current = case.source_tokens[index]
    used = set(case.source_tokens)
    for token in case.source_vocab:
        if token != current and token not in used:
            return token
    for token in reversed(case.source_vocab):
        if token != current:
            return token
    return current


def make_sample_bundle(seed: int = 20261120) -> dict[str, Any]:
    case = make_sample_case(seed)
    unaligned = unaligned_source_indices(case)
    changed = (
        case.with_source_token(
            unaligned[0], modification_token_for(case, unaligned[0])
        )
        if unaligned
        else case
    )
    return {
        "schema_version": 1,
        "generator": "mtstream.sample_data.make_sample_case",
        "seed": seed,
        "case": case.to_dict(),
        "semantics": {
            "source_encoding": "SHA-256 seeded embedding + position weighted pooling",
            "sampling": "deterministic greedy top-1 with token lexical tie-break",
            "unaligned_source_indices": list(unaligned),
        },
        "unreferenced_source_modification": changed.to_dict(),
    }

