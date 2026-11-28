"""Deterministic local speculative-decoding verification toolkit."""

from .model import (
    ACCEPTANCE_MODE,
    DRAFT_VOCAB_SIZE,
    GENERATION_TOKEN_IDS,
    SPECIAL_IDS,
    ModelSuite,
    build_token_mapping,
)

__all__ = [
    "ACCEPTANCE_MODE",
    "DRAFT_VOCAB_SIZE",
    "GENERATION_TOKEN_IDS",
    "SPECIAL_IDS",
    "ModelSuite",
    "build_token_mapping",
]

