"""Deterministic stroke-mask glyph templates.

The library is self-contained: no font file or network asset is used. Each
glyph is a 5x7 stroke mask and is rasterized with nearest-neighbour scaling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .errors import BusinessError, E_UNKNOWN_CHARSET_VERSION
from .serialization import checksum


LATIN_CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_RAW_PATTERNS: dict[str, tuple[str, ...]] = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}

_BASE_PATTERNS = {
    character: tuple(row for row in rows) for character, rows in _RAW_PATTERNS.items()
}

_VERSION_CHANGES: dict[str, dict[str, tuple[str, ...]]] = {
    "glyph-v1": {},
    "glyph-v2": {
        "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01111"),
        "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01100"),
    },
}


def _pattern_matrix(rows: tuple[str, ...]) -> np.ndarray:
    return np.asarray([[1 if cell == "1" else 0 for cell in row] for row in rows], dtype=np.uint8)


def _resize_nearest(mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    target_h, target_w = shape
    source_h, source_w = mask.shape
    y_index = (np.arange(target_h, dtype=np.int64) * source_h) // max(target_h, 1)
    x_index = (np.arange(target_w, dtype=np.int64) * source_w) // max(target_w, 1)
    return mask[y_index[:, None], x_index[None, :]]


def rasterize_pattern(
    rows: tuple[str, ...],
    shape: tuple[int, int] = (21, 15),
) -> np.ndarray:
    """Rasterize a 5x7 stroke mask into a fixed shape without interpolation."""
    return _resize_nearest(_pattern_matrix(rows), shape)


@dataclass(frozen=True)
class TemplateLibrary:
    version: str
    charset_versions: dict[str, str]
    patterns: dict[str, tuple[str, ...]]
    high_shape: tuple[int, int] = (21, 15)
    low_shape: tuple[int, int] = (12, 8)

    @property
    def checksum(self) -> str:
        return checksum(
            {
                "version": self.version,
                "charset_versions": self.charset_versions,
                "patterns": self.patterns,
                "high_shape": self.high_shape,
                "low_shape": self.low_shape,
            }
        )

    @property
    def supported_charset_versions(self) -> tuple[str, ...]:
        return tuple(sorted(self.charset_versions))

    def charset_for(self, charset_version: str) -> str:
        try:
            return self.charset_versions[charset_version]
        except KeyError as exc:
            raise BusinessError(
                E_UNKNOWN_CHARSET_VERSION,
                f"unknown charset_version: {charset_version}",
                stage="recognize",
                field="charset_version",
                details={"charset_version": charset_version},
            ) from exc

    def require_charset(self, charset_version: str) -> str:
        return self.charset_for(charset_version)

    def pattern(self, character: str) -> tuple[str, ...]:
        try:
            return self.patterns[character]
        except KeyError as exc:
            raise KeyError(f"unsupported character: {character}") from exc

    def high_template(self, character: str) -> np.ndarray:
        return rasterize_pattern(self.pattern(character), self.high_shape)

    def low_template(self, character: str) -> np.ndarray:
        return rasterize_pattern(self.pattern(character), self.low_shape)

    def stack(self, characters: Iterable[str], *, low: bool = False) -> np.ndarray:
        ordered = sorted(set(characters))
        if low:
            return np.stack([self.low_template(char) for char in ordered]).astype(np.float64)
        return np.stack([self.high_template(char) for char in ordered]).astype(np.float64)

    def changed_characters(self, other: "TemplateLibrary") -> tuple[str, ...]:
        changed = []
        for character in sorted(set(self.patterns) | set(other.patterns)):
            if self.patterns.get(character) != other.patterns.get(character):
                changed.append(character)
        return tuple(changed)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "charset_versions": self.charset_versions,
            "high_shape": list(self.high_shape),
            "low_shape": list(self.low_shape),
            "changed_from_v1": sorted(self.changed_characters(get_template_library("glyph-v1"))),
        }


def get_template_library(version: str = "glyph-v1") -> TemplateLibrary:
    if version not in _VERSION_CHANGES:
        raise BusinessError(
            E_UNKNOWN_CHARSET_VERSION,
            f"unknown template library version: {version}",
            stage="recognize",
            field="template_version",
            details={"template_version": version},
        )
    overrides = _VERSION_CHANGES[version]
    patterns = dict(_BASE_PATTERNS)
    patterns.update(overrides)
    return TemplateLibrary(
        version=version,
        charset_versions={"latin-v1": LATIN_CHARSET},
        patterns=patterns,
        high_shape=(21, 15),
        low_shape=(12, 8),
    )

