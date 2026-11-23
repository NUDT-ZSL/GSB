"""Controlled-vocabulary tokenization and deterministic text embeddings."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from .canonical import digest, f64_b64
from .errors import BusinessError, E_VOCAB_MISSING, E_VOCAB_OUT_OF_RANGE
from .models import TEXT_EMBEDDING_DIM, TEXT_VERSION, Vocabulary


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _is_separator(character: str) -> bool:
    return character.isspace() or unicodedata.category(character).startswith("P")


def tokenize_text(text: str, vocabulary: Vocabulary, pair_id: str | None = None) -> List[str]:
    vocabulary.validate()
    normalized = _normalize_text(text)
    terms = sorted(
        {term.casefold(): term.casefold() for term in vocabulary.terms}.items(),
        key=lambda item: (-len(item[0]), item[0]),
    )
    lookup = [item[0] for item in terms]
    tokens: List[str] = []
    index = 0
    while index < len(normalized):
        if _is_separator(normalized[index]):
            index += 1
            continue
        match = None
        for term in lookup:
            if normalized.startswith(term, index):
                match = term
                break
        if match is None:
            end = index
            while end < len(normalized) and not _is_separator(normalized[end]):
                end += 1
            unknown = normalized[index:end]
            raise BusinessError(
                E_VOCAB_MISSING,
                "文本包含受控词表缺失的词项",
                pair_id=pair_id,
                field="text",
                details={"token": unknown, "character_offset": index},
            )
        tokens.append(match)
        index += len(match)
    if not tokens:
        raise BusinessError(
            E_VOCAB_MISSING,
            "文本切分后没有可用的受控词项",
            pair_id=pair_id,
            field="text",
        )
    return tokens


def validate_text_coverage(samples: Sequence[Any], vocabulary: Vocabulary) -> None:
    for sample in samples:
        tokenize_text(str(sample.text), vocabulary, str(sample.pair_id))


def term_embedding(term: str, vocab_version: int, dimension: int = TEXT_EMBEDDING_DIM) -> np.ndarray:
    vector = np.empty(dimension, dtype=np.float64)
    prefix = f"{TEXT_VERSION}|{vocab_version}|{term}|".encode("utf-8")
    for index in range(dimension):
        raw = hashlib.blake2b(prefix + str(index).encode("ascii"), digest_size=8).digest()
        integer = int.from_bytes(raw, "little", signed=False)
        vector[index] = (integer / float(2**63)) - 1.0
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float64) if norm else np.zeros(dimension, dtype=np.float64)


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float64) if norm else np.zeros_like(vector, dtype=np.float64)


def embed_text(
    text: str,
    vocabulary: Vocabulary,
    vocab_version: int,
    pair_id: str | None = None,
) -> Dict[str, Any]:
    tokens = tokenize_text(text, vocabulary, pair_id)
    frequencies: Dict[str, int] = {}
    for token in tokens:
        frequencies[token] = frequencies.get(token, 0) + 1
    total = float(len(tokens))
    pooled = np.zeros(TEXT_EMBEDDING_DIM, dtype=np.float64)
    weights: List[Dict[str, Any]] = []
    for token, count in sorted(frequencies.items()):
        weight = count / total
        vector = term_embedding(token, vocab_version)
        pooled += weight * vector
        weights.append({"term": token, "count": count, "weight": format(weight, ".17g")})
    embedding = _unit(pooled)
    artifact = {
        "algorithm": TEXT_VERSION,
        "tokenizer": vocabulary.tokenizer,
        "vocab_version": vocab_version,
        "embedding_dim": TEXT_EMBEDDING_DIM,
        "tokens": tokens,
        "term_weights": weights,
        "vector_f64_b64": f64_b64(embedding),
    }
    artifact["artifact_hash"] = digest(artifact)
    return artifact


def vector_from_text_artifact(artifact: Dict[str, Any]) -> np.ndarray:
    from .canonical import b64_f64

    return b64_f64(artifact["vector_f64_b64"])


def out_of_range_token(vocabulary: Vocabulary, token_index: int, pair_id: str | None = None) -> None:
    if token_index < 0 or token_index >= len(vocabulary.terms):
        raise BusinessError(
            E_VOCAB_OUT_OF_RANGE,
            "词项索引越界",
            pair_id=pair_id,
            field="token_index",
            details={"token_index": token_index, "vocab_size": len(vocabulary.terms)},
        )
