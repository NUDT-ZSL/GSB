"""Deterministic stroke-template library.

Each glyph is a small binary bitmap synthesized from a fixed-seed RNG.  The
bitmap is post-processed so every column and every row contains at least one
dark pixel; this keeps glyphs visually connected and guarantees stable
projection/connected-component detection.

A library carries:
  * a monotonic integer ``library_version``,
  * a ``charset_version`` tag,
  * an ordered alphabet,
  * one H x W template per character,
  * a SHA-256 checksum over all of the above in canonical form.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .util import content_hash, rng_from_seed

GLYPH_HEIGHT = 12
GLYPH_WIDTH = 10

# Ordered alphabets.  v2 intentionally *shrinks* the alphabet (two glyphs are
# retired), which exercises charset-shrink cache invalidation.
CHARSETS: dict[str, str] = {
    "cs-v1": "ABCDEF0123",
    "cs-v2": "ABCDE012",
}


@dataclass(frozen=True)
class TemplateLibrary:
    library_version: int
    charset_version: str
    alphabet: str
    templates: np.ndarray  # shape (len(alphabet), GLYPH_HEIGHT, GLYPH_WIDTH), uint8 0/1
    checksum: str

    @property
    def glyph_count(self) -> int:
        return len(self.alphabet)

    def index_of(self, char: str) -> int:
        return self.alphabet.index(char)

    def to_json(self) -> dict:
        return {
            "library_version": self.library_version,
            "charset_version": self.charset_version,
            "alphabet": self.alphabet,
            "glyph_height": GLYPH_HEIGHT,
            "glyph_width": GLYPH_WIDTH,
            "templates": [row.tolist() for row in self.templates],
            "checksum": self.checksum,
        }

    @classmethod
    def from_json(cls, payload: dict) -> "TemplateLibrary":
        templates = np.asarray(payload["templates"], dtype=np.uint8)
        expected = _library_checksum(
            payload["library_version"], payload["charset_version"],
            payload["alphabet"], templates,
        )
        if expected != payload["checksum"]:
            from .errors import SnapshotCorruptError
            raise SnapshotCorruptError(
                "template library checksum mismatch",
                {"category": "template_library",
                 "version": payload["library_version"],
                 "expected": expected, "actual": payload["checksum"]},
            )
        return cls(
            library_version=int(payload["library_version"]),
            charset_version=payload["charset_version"],
            alphabet=payload["alphabet"],
            templates=templates,
            checksum=expected,
        )


def _build_glyph(char: str, charset_version: str, library_version: int) -> np.ndarray:
    seed = int.from_bytes(
        f"glyph|{charset_version}|{library_version}|{char}".encode("utf-8"), "big"
    ) & 0x7FFFFFFF
    rng = rng_from_seed(seed, GLYPH_HEIGHT, GLYPH_WIDTH)
    # Distinctive stroke field, deterministic.  The sparser fill keeps glyphs
    # separable under template matching while remaining visually connected.
    bitmap = (rng.random((GLYPH_HEIGHT, GLYPH_WIDTH)) < 0.42).astype(np.uint8)
    # A fixed deterministic vertical stroke anchors every glyph's left edge,
    # giving connected components a stable left boundary.
    bitmap[:, 0] = 1
    # A deterministic horizontal crossbar differentiates glyphs.
    crossbar = 2 + (seed % (GLYPH_HEIGHT - 4))
    bitmap[crossbar, :] = 1
    # Guarantee every column/row has a dark pixel so glyphs stay connected.
    bitmap[:, np.where(bitmap.sum(axis=0) == 0)[0]] = 1
    bitmap[np.where(bitmap.sum(axis=1) == 0)[0], :] = 1
    return bitmap


def _library_checksum(
    library_version: int, charset_version: str, alphabet: str,
    templates: np.ndarray,
) -> str:
    body = {
        "library_version": library_version,
        "charset_version": charset_version,
        "alphabet": alphabet,
        "glyph_height": GLYPH_HEIGHT,
        "glyph_width": GLYPH_WIDTH,
        "templates": templates.astype(np.uint8).tolist(),
    }
    return content_hash(body)


def build_library(charset_version: str, library_version: int) -> TemplateLibrary:
    if charset_version not in CHARSETS:
        from .errors import ConfigError
        raise ConfigError(
            "unknown charset version", {"charset_version": charset_version}
        )
    alphabet = CHARSETS[charset_version]
    templates = np.stack(
        [_build_glyph(char, charset_version, library_version) for char in alphabet]
    ).astype(np.uint8)
    checksum = _library_checksum(library_version, charset_version, alphabet, templates)
    return TemplateLibrary(
        library_version=library_version,
        charset_version=charset_version,
        alphabet=alphabet,
        templates=templates,
        checksum=checksum,
    )


def subset_library(
    library: TemplateLibrary, retained_alphabet: str, new_version: int
) -> TemplateLibrary:
    """Return a library containing only ``retained_alphabet`` glyphs."""
    indices = [library.alphabet.index(char) for char in retained_alphabet]
    templates = library.templates[np.asarray(indices, dtype=np.int64)]
    charset_version = f"{library.charset_version}-shrink{new_version}"
    checksum = _library_checksum(
        new_version, charset_version, retained_alphabet, templates
    )
    return TemplateLibrary(
        library_version=new_version,
        charset_version=charset_version,
        alphabet=retained_alphabet,
        templates=templates,
        checksum=checksum,
    )
