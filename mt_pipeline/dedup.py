"""Dedup stage: exact (exhaustive key compare) vs approx (signature digest)."""

from .util import canonical_bytes, sha256_hex


def _key_bytes(rec):
    return canonical_bytes([rec["src_tokens"], rec["tgt_tokens"]])


def dedup_exact(records):
    """Exhaustive: full normalized key comparison, keep first (lowest pair_id)."""
    seen = set()
    dropped = {}
    for rec in records:
        key = _key_bytes(rec)
        if key in seen:
            dropped[rec["pair_id"]] = "DUPLICATE"
        else:
            seen.add(key)
    return dropped


def dedup_signature(records):
    """Approx: 64-bit signature digest of the normalized key."""
    seen = set()
    dropped = {}
    for rec in records:
        sig = sha256_hex(_key_bytes(rec))[:16]
        if sig in seen:
            dropped[rec["pair_id"]] = "DUPLICATE"
        else:
            seen.add(sig)
    return dropped


def dedup(records, path):
    return dedup_exact(records) if path == "exact" else dedup_signature(records)
