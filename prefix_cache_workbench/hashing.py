"""Deterministic hashing and hash-embedding primitives.

All fingerprints derive from SHA-256 over explicit, version-prefixed byte
strings so that identical text always produces identical fingerprints on every
machine and every run, regardless of dictionary insertion order or thread
scheduling.  Embeddings use NumPy with fixed seeds.
"""

import hashlib
import json

import numpy as np

FINGERPRINT_VERSION = "fp-v1"


def canonical_json(payload):
    """JSON serialization with sorted keys and fixed separators."""
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def sha256_hex(label, payload_text):
    material = f"{FINGERPRINT_VERSION}|{label}|{payload_text}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def config_fingerprint(config):
    return sha256_hex("config", canonical_json(config))


def vocab_fingerprint(vocab_rules):
    return sha256_hex("vocab", canonical_json(vocab_rules))


def content_fingerprint(token_ids, derive):
    """Content fingerprint of one block; position independent by design."""
    payload = canonical_json(
        {
            "tokens": list(token_ids),
            "hash_algo": derive.get("hash_algo", "SHA256"),
            "content_prefix": derive.get("content_prefix", "C"),
            "version": derive.get("version", FINGERPRINT_VERSION),
        }
    )
    return sha256_hex("content", payload)


def chain_fingerprint(content_fp, parent_fp, derive):
    """Chain fingerprint binds block content to its parent fingerprint."""
    payload = canonical_json(
        {
            "content_fp": content_fp,
            "parent_fp": parent_fp,
            "chain_prefix": derive.get("chain_prefix", "P"),
            "hash_algo": derive.get("hash_algo", "SHA256"),
            "version": derive.get("version", FINGERPRINT_VERSION),
        }
    )
    return sha256_hex("chain", payload)


class EmbeddingEncoder:
    """Deterministic hash embedding based on NumPy bit generators.

    Each token id maps to a fixed-size float32 vector.  The vector only depends
    on the token id, the configured seed and the embedding dimension, so it is
    reproducible across processes and platforms.
    """

    def __init__(self, vocab_rules):
        emb = vocab_rules.get("embedding", {})
        self.seed = int(emb.get("seed", 20261117))
        self.dim = int(emb.get("dim", 8))
        self.scale = float(emb.get("scale", 1.0))
        self._cache = {}

    def embed_token(self, token_id):
        if token_id not in self._cache:
            bg = np.random.default_rng(
                np.random.SeedSequence([self.seed, int(token_id)])
            )
            self._cache[token_id] = bg.standard_normal(self.dim, np.float32)
        return self._cache[token_id]

    def block_embedding(self, token_ids):
        vectors = np.stack(
            [self.embed_token(int(i)) for i in token_ids], axis=0
        )
        mean = np.round(vectors.mean(axis=0), 6)
        return (mean * self.scale).astype(np.float32)

    def block_digest(self, token_ids):
        vector = self.block_embedding(token_ids)
        return [float(round(float(v), 6)) for v in vector]
