"""Deterministic helpers: canonical JSON, checksums, PRNG, codepoints."""

import hashlib
import json
from pathlib import Path


def canonical_bytes(obj):
    """Canonical serialization used for checksums and byte-identical output."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def checksum_obj(obj):
    return sha256_hex(canonical_bytes(obj))


def write_json(path, obj):
    """Deterministic pretty JSON (sorted keys, trailing newline)."""
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def codepoint_interval(tokens):
    """[min, max] Unicode codepoint over all characters of the token sequence."""
    lo, hi = None, None
    for tok in tokens:
        for ch in tok:
            cp = ord(ch)
            lo = cp if lo is None else min(lo, cp)
            hi = cp if hi is None else max(hi, cp)
    if lo is None:
        return [0, 0]
    return [lo, hi]


class Splitmix64:
    """Pure-python deterministic PRNG (stable across platforms/versions)."""

    _MASK = 0xFFFFFFFFFFFFFFFF

    def __init__(self, seed):
        self.state = seed & self._MASK

    def next_u64(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & self._MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & self._MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & self._MASK
        return z ^ (z >> 31)

    def randint(self, a, b):
        """Inclusive integer in [a, b]."""
        return a + self.next_u64() % (b - a + 1)

    def choice(self, seq):
        return seq[self.randint(0, len(seq) - 1)]
