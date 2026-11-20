"""Deterministic character stroke template library with versioning.

Each character is represented by a fixed-size binary glyph rendered by a
deterministic stroke template.  Glyphs are generated purely from stable seeds
derived from the character code and the library version, so a given
``(charset_version, character)`` always produces identical pixels.

The library carries a monotonically increasing version number and a content
checksum over every glyph.  Bumping the version regenerates the whole library
(a full snapshot replacement), it never patches individual glyphs in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np

from .deterministic import content_hash, hash_bytes, seeded_rng

GLYPH_W = 5
GLYPH_H = 7

# 36-symbol charset: digits then upper-case letters.  Fixed ordering is part of
# the deterministic contract and used as the candidate comparison order.
CHARSET: Tuple[str, ...] = tuple("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _base_glyph(char: str) -> np.ndarray:
    """Render the version-1 deterministic stroke template for ``char``."""

    rng = seeded_rng("glyph-base", ord(char))
    glyph = np.zeros((GLYPH_H, GLYPH_W), dtype=np.uint8)
    # A vertical spine plus seeded horizontal strokes yields distinct, stable
    # and readable block-letter style templates per character.
    glyph[:, 0] = 255
    stroke_count = 2 + (ord(char) % 3)
    for _ in range(stroke_count):
        row = int(rng.integers(0, GLYPH_H))
        length = int(rng.integers(2, GLYPH_W + 1))
        glyph[row, :length] = 255
    # Deterministic serif dots add distinguishing structure.
    serif = int(rng.integers(0, GLYPH_H))
    glyph[serif, GLYPH_W - 1] = 255
    return glyph


def _versioned_glyph(char: str, version: int) -> np.ndarray:
    """Return the glyph for a library version.

    Version 1 is the base template.  Higher versions apply a deterministic,
    version-scoped mutation to the whole library so every glyph changes in a
    reproducible way (simulating a real template revision).  The entire set is
    regenerated rather than mutating a stored snapshot in place.
    """

    glyph = _base_glyph(char).copy()
    if version <= 1:
        return glyph
    rng = seeded_rng("glyph-revision", version, ord(char))
    # Flip a fixed, version-dependent pair of pixels for every glyph.
    for _ in range(2):
        row = int(rng.integers(0, GLYPH_H))
        col = int(rng.integers(0, GLYPH_W))
        glyph[row, col] = 0 if glyph[row, col] == 255 else 255
    return glyph


@dataclass(frozen=True)
class TemplateLibrary:
    """An immutable whole-library snapshot for one charset version."""

    version: int
    checksum: str
    glyphs: Dict[str, np.ndarray]
    charset: Tuple[str, ...] = CHARSET

    def glyph(self, char: str) -> np.ndarray:
        return self.glyphs[char]

    def ordered_glyphs(self) -> List[np.ndarray]:
        return [self.glyphs[ch] for ch in self.charset]

    def to_payload(self) -> Dict[str, object]:
        """Plain JSON-serialisable representation used for snapshot storage."""

        return {
            "version": self.version,
            "checksum": self.checksum,
            "charset": list(self.charset),
            "glyphs": {ch: self.glyphs[ch].tolist() for ch in self.charset},
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, object]) -> "TemplateLibrary":
        version = int(payload["version"])
        checksum = str(payload["checksum"])
        chars = tuple(payload["charset"])  # type: ignore[arg-type]
        glyphs = {
            ch: np.asarray(payload["glyphs"][ch], dtype=np.uint8)  # type: ignore[index]
            for ch in chars
        }
        rebuilt = compute_checksum(version, glyphs, chars)
        if rebuilt != checksum:
            raise ValueError("checksum-mismatch")
        return cls(version=version, checksum=checksum, glyphs=glyphs, charset=chars)


def compute_checksum(
    version: int, glyphs: Dict[str, np.ndarray], charset: Sequence[str]
) -> str:
    """Content checksum covering the version and every glyph's bytes."""

    hasher_parts: List[object] = [version]
    for ch in charset:
        hasher_parts.append(ch)
        hasher_parts.append(hash_bytes(np.asarray(glyphs[ch], dtype=np.uint8).tobytes()))
    return content_hash(hasher_parts)


def build_library(version: int) -> TemplateLibrary:
    """Build the complete immutable template snapshot for ``version``."""

    if version < 1:
        raise ValueError("template version must be >= 1")
    glyphs = {ch: _versioned_glyph(ch, version) for ch in CHARSET}
    checksum = compute_checksum(version, glyphs, CHARSET)
    return TemplateLibrary(version=version, checksum=checksum, glyphs=glyphs)
