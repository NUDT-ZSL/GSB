from __future__ import annotations

from .models import Token


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _is_word_character(character: str) -> bool:
    return character.isascii() and (character.isalnum() or character == "_")


def tokenize(text: str) -> list[Token]:
    """Deterministic mixed Chinese/ASCII tokenizer.

    Han characters are emitted one code point per token, contiguous ASCII
    alphanumerics are grouped, and punctuation is emitted as its own token.
    Whitespace is ignored. Offsets are Python code-point offsets.
    """

    tokens: list[Token] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character.isspace():
            index += 1
            continue
        if _is_word_character(character):
            start = index
            index += 1
            while index < len(text) and _is_word_character(text[index]):
                index += 1
            tokens.append(Token(start, index, text[start:index], "word"))
            continue
        if _is_cjk(character):
            tokens.append(Token(index, index + 1, character, "han"))
            index += 1
            continue
        tokens.append(Token(index, index + 1, character, "punct"))
        index += 1
    return tokens

