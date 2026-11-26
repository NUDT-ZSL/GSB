from __future__ import annotations

import unicodedata
from typing import Any


def normalize_sentence(
    sentence_id: str, raw_text: str, config: dict[str, Any]
) -> dict[str, Any]:
    settings = config["normalization"]
    unicode_form = settings.get("unicode_form", "NFKC")
    case_mode = settings.get("case_mode", "casefold")
    whitespace_mode = settings.get("whitespace_mode", "collapse")
    replacements = settings.get("domain_replacements", {})

    transformed: list[tuple[str, int, int]] = []
    for raw_index, original in enumerate(raw_text):
        value = original
        if unicode_form != "none":
            value = unicodedata.normalize(unicode_form, value)
        for source_char in value:
            replaced = replacements.get(source_char, source_char)
            if case_mode == "casefold":
                replaced = replaced.casefold()
            elif case_mode == "lower":
                replaced = replaced.lower()
            for output_char in replaced:
                transformed.append((output_char, raw_index, raw_index + 1))

    normalized_chars: list[str] = []
    char_map: list[list[int]] = []
    index = 0
    while index < len(transformed):
        char, raw_start, raw_end = transformed[index]
        if char.isspace() and whitespace_mode == "collapse":
            run_end = index + 1
            merged_raw_end = raw_end
            while run_end < len(transformed) and transformed[run_end][0].isspace():
                merged_raw_end = transformed[run_end][2]
                run_end += 1
            normalized_chars.append(" ")
            char_map.append([raw_start, merged_raw_end])
            index = run_end
            continue
        normalized_chars.append(char)
        char_map.append([raw_start, raw_end])
        index += 1

    return {
        "sentence_id": sentence_id,
        "raw_text": raw_text,
        "normalized_text": "".join(normalized_chars),
        "char_map": char_map,
        "normalization": {
            "locale": config["locale"],
            "domain": config["domain"],
            "unicode_form": unicode_form,
            "case_mode": case_mode,
            "whitespace_mode": whitespace_mode,
            "domain_replacements": replacements,
        },
    }

