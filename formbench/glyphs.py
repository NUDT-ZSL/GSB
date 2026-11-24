from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import VocabularyMissingError


FONT_3X5: dict[str, tuple[str, ...]] = {
    "A": ("010", "101", "111", "101", "101"),
    "B": ("110", "101", "110", "101", "110"),
    "C": ("111", "100", "100", "100", "111"),
    "D": ("110", "101", "101", "101", "110"),
    "E": ("111", "100", "110", "100", "111"),
    "F": ("111", "100", "110", "100", "100"),
    "G": ("111", "100", "101", "101", "111"),
    "H": ("101", "101", "111", "101", "101"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("001", "001", "001", "101", "111"),
    "K": ("101", "110", "100", "110", "101"),
    "L": ("100", "100", "100", "100", "111"),
    "M": ("101", "111", "111", "101", "101"),
    "N": ("101", "111", "111", "111", "101"),
    "O": ("010", "101", "101", "101", "010"),
    "P": ("110", "101", "110", "100", "100"),
    "Q": ("010", "101", "101", "110", "011"),
    "R": ("110", "101", "110", "110", "101"),
    "S": ("111", "100", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
    "U": ("101", "101", "101", "101", "111"),
    "V": ("101", "101", "101", "101", "010"),
    "W": ("101", "101", "111", "111", "101"),
    "X": ("101", "101", "010", "101", "101"),
    "Y": ("101", "101", "010", "010", "010"),
    "Z": ("111", "001", "010", "100", "111"),
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("110", "001", "010", "100", "111"),
    "3": ("110", "001", "010", "001", "110"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "110", "001", "110"),
    "6": ("011", "100", "110", "101", "010"),
    "7": ("111", "001", "010", "010", "010"),
    "8": ("010", "101", "010", "101", "010"),
    "9": ("010", "101", "011", "001", "110"),
    "-": ("000", "000", "111", "000", "000"),
    "_": ("000", "000", "000", "000", "111"),
    ".": ("000", "000", "000", "000", "010"),
    ":": ("000", "010", "000", "010", "000"),
    "/": ("001", "001", "010", "100", "100"),
    "?": ("110", "001", "010", "000", "010"),
}


@dataclass(frozen=True)
class Template:
    text: str
    style: str
    bitmap: np.ndarray

    @property
    def height(self) -> int:
        return int(self.bitmap.shape[0])

    @property
    def width(self) -> int:
        return int(self.bitmap.shape[1])


def normalize_text(text: str) -> str:
    return " ".join(str(text).upper().split())


def text_width(text: str, cell: int = 8) -> int:
    text = normalize_text(text)
    width = 0
    for index, char in enumerate(text):
        if char == " ":
            width += 3 * cell
            continue
        if char not in FONT_3X5:
            raise VocabularyMissingError(
                f"no deterministic glyph for vocabulary item {char!r}",
                field="vocabulary",
                reason_code="E_VOCAB_GLYPH_MISSING",
                details={"character": char, "text": text},
            )
        width += 3 * cell
        if index < len(text) - 1:
            width += cell
    return max(1, width)


def render_word_mask(
    text: str,
    *,
    cell: int = 8,
    bold: bool = False,
    jitter_rng: np.random.RandomState | None = None,
    handwritten: bool = False,
) -> np.ndarray:
    """Render a word to a tight boolean mask using the deterministic font."""
    text = normalize_text(text)
    width = text_width(text, cell)
    height = 5 * cell
    mask = np.zeros((height, width), dtype=bool)
    x = 0
    for char in text:
        if char == " ":
            x += 3 * cell
            continue
        glyph = FONT_3X5[char]
        dx = 0
        dy = 0
        if handwritten and jitter_rng is not None:
            dx = int(jitter_rng.randint(-1, 2)) * max(1, cell // 8)
            dy = int(jitter_rng.randint(-1, 2)) * max(1, cell // 8)
        for row, line in enumerate(glyph):
            for col, mark in enumerate(line):
                if mark != "1":
                    continue
                x0 = x + col * cell + dx
                y0 = row * cell + dy
                x1 = x0 + cell + (1 if bold else 0)
                y1 = y0 + cell + (1 if bold else 0)
                xa, xb = max(0, x0), min(width, x1)
                ya, yb = max(0, y0), min(height, y1)
                if xa < xb and ya < yb:
                    mask[ya:yb, xa:xb] = True
        x += 3 * cell + cell
    coords = np.argwhere(mask)
    if coords.size:
        y0, x0 = coords.min(axis=0)
        y1, x1 = coords.max(axis=0) + 1
        mask = mask[y0:y1, x0:x1]
    return mask


def build_templates(
    vocabulary: list[str],
    *,
    cell: int = 8,
    missing_words: set[str] | None = None,
) -> list[Template]:
    templates: list[Template] = []
    missing_words = {normalize_text(word) for word in missing_words or set()}
    for raw_word in vocabulary:
        word = normalize_text(raw_word)
        if not word:
            continue
        if word in missing_words:
            raise VocabularyMissingError(
                f"injected missing vocabulary template for {word!r}",
                field="exception_injections",
                reason_code="E_VOCAB_MISSING_INJECTED",
                details={"word": word},
            )
        for style, bold in (("regular", False), ("bold", True)):
            bitmap = render_word_mask(word, cell=cell, bold=bold).astype(np.uint8)
            templates.append(Template(text=word, style=style, bitmap=bitmap))
    if not templates:
        raise VocabularyMissingError("vocabulary produced no templates", field="vocabulary")
    return templates
