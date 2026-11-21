"""Bottom-k signature generation and signature Jaccard similarity.

Tokenization contract:
1. Unicode NFKC normalization and case folding.
2. Tokens are maximal Unicode word runs (``\\w+``).
3. Prompt, chosen, and rejected are joined with newlines before tokenization.
4. The shingle set contains Unicode word n-grams. Documents shorter than n use
   their complete token tuple as the only shingle.
5. Each shingle is keyed with BLAKE2b-64 and the lowest-k distinct hashes form
   the signature.
6. Similarity is the exact Jaccard ratio of the two bottom-k signature sets.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .textutil import ngrams, sample_text, tokenize


SIGNATURE_HASH_BITS = 64


def shingle_hashes(sample: dict[str, Any], shingle_size: int, seed: int) -> set[int]:
    grams = ngrams(tokenize(sample_text(sample)), shingle_size)
    hashes: set[int] = set()
    seed_bytes = str(seed).encode("utf-8")
    for gram in grams:
        payload = seed_bytes + b"\x00" + "\x1f".join(gram).encode("utf-8")
        digest = hashlib.blake2b(payload, digest_size=8).digest()
        hashes.add(int.from_bytes(digest, "big"))
    return hashes


def signature(sample: dict[str, Any], shingle_size: int, signature_size: int, seed: int) -> list[int]:
    hashes = shingle_hashes(sample, shingle_size, seed)
    return sorted(hashes)[:signature_size]


def signature_jaccard(left: list[int], right: list[int]) -> tuple[float, int, int]:
    left_set = set(left)
    right_set = set(right)
    union = left_set | right_set
    intersection = left_set & right_set
    similarity = len(intersection) / len(union) if union else 0.0
    return similarity, len(intersection), len(union)


def signature_params(signature_config: dict[str, Any]) -> dict[str, Any]:
    return {
        "hash": "blake2b-64",
        "hash_bits": SIGNATURE_HASH_BITS,
        "seed": signature_config["signature_seed"],
        "size": signature_config["signature_size"],
        "shingle_size": signature_config["shingle_size"],
        "similarity": "exact_jaccard_of_bottom_k_signature_sets",
        "tie_break": "pair_id_ascending",
    }

