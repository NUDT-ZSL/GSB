"""Context assembly, validation and block segmentation."""

from .errors import (
    E_CONTEXT_TOO_LONG,
    E_EMPTY_CONTEXT,
    E_TOKEN_UNKNOWN,
    WorkbenchError,
)
from .hashing import chain_fingerprint, content_fingerprint


class Vocabulary:
    """Controlled vocabulary with stable integer token ids."""

    def __init__(self, vocab_rules):
        self.rules = vocab_rules
        self.specials = list(vocab_rules.get("special_tokens", []))
        words = list(vocab_rules.get("words", []))
        reserved = len(self.specials)
        self.token_to_id = {tok: idx for idx, tok in enumerate(self.specials)}
        for word in words:
            if word in self.token_to_id:
                raise ValueError(f"duplicate vocabulary token: {word}")
            self.token_to_id[word] = reserved
            reserved += 1
        self.id_to_token = {idx: tok for tok, idx in self.token_to_id.items()}
        limits = vocab_rules.get("limits", {})
        self.max_context_tokens = int(limits.get("max_context_tokens", 4096))

    def size(self):
        return len(self.token_to_id)

    def tokenize(self, text, stage):
        tokens = text.split()
        ids = []
        for token in tokens:
            if token not in self.token_to_id:
                raise WorkbenchError(
                    E_TOKEN_UNKNOWN,
                    stage,
                    f"token not in controlled vocabulary: {token}",
                )
            ids.append(self.token_to_id[token])
        return ids


def assemble_context(request, vocab):
    """Assemble the full context token sequence for one request."""
    parts = [
        ("system", request.get("system_prompt", "")),
        ("history", request.get("history", "")),
        ("question", request.get("question", "")),
    ]
    assembled = []
    for stage, text in parts:
        if text is None:
            text = ""
        ids = vocab.tokenize(text, f"tokenize:{stage}")
        assembled.extend(ids)
    if not assembled:
        raise WorkbenchError(
            E_EMPTY_CONTEXT, "assemble", "assembled context contains no token"
        )
    if len(assembled) > vocab.max_context_tokens:
        raise WorkbenchError(
            E_CONTEXT_TOO_LONG,
            "assemble",
            f"context length {len(assembled)} exceeds "
            f"{vocab.max_context_tokens}",
        )
    return assembled


def split_blocks(token_ids, block_tokens):
    """Fixed-size segmentation; the final block may be shorter."""
    return [
        token_ids[start : start + block_tokens]
        for start in range(0, len(token_ids), block_tokens)
    ]


def build_block_sequence(token_ids, config, vocab):
    """Pure deterministic construction from an empty cache.

    Returns block dicts with content fingerprints, chain fingerprints,
    boundaries and numpy hash-embedding digests.
    """
    block_tokens = int(config["block_tokens"])
    derive = config["hash_derive"]
    blocks = []
    parent_fp = None
    for index, chunk in enumerate(split_blocks(token_ids, block_tokens)):
        content_fp = content_fingerprint(chunk, derive)
        chain_fp = chain_fingerprint(content_fp, parent_fp, derive)
        blocks.append(
            {
                "index": index,
                "token_ids": list(chunk),
                "start": index * block_tokens,
                "end": index * block_tokens + len(chunk),
                "content_fp": content_fp,
                "parent_fp": parent_fp,
                "chain_fp": chain_fp,
            }
        )
        parent_fp = chain_fp
    return blocks
