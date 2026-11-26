from __future__ import annotations

import re
from typing import Any


def _applies(item: dict[str, Any], config: dict[str, Any]) -> bool:
    domains = item.get("domains") or [config["domain"]]
    locales = item.get("locales") or [config["locale"]]
    return config["domain"] in domains and config["locale"] in locales


def _candidate(
    *,
    start: int,
    end: int,
    text: str,
    source: str,
    source_version: str | None,
    source_id: str | None,
    tag_hint: str,
    priority: int | float,
) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "text": text,
        "source": source,
        "source_version": source_version,
        "source_id": source_id,
        "tag_hint": tag_hint,
        "priority": priority,
    }


def generate_candidates(
    normalized: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    text = normalized["normalized_text"]
    priorities = config["disambiguation"]["priority"]
    candidates: list[dict[str, Any]] = []

    dictionary = [
        entry for entry in config["dictionary"] if _applies(entry, config)
    ]
    dictionary.sort(
        key=lambda entry: (
            -len(entry["term"]),
            entry["term"],
            entry["entry_id"],
        )
    )
    for start in range(len(text)):
        if text[start].isspace():
            continue
        for entry in dictionary:
            term = entry["term"]
            if text.startswith(term, start):
                end = start + len(term)
                candidates.append(
                    _candidate(
                        start=start,
                        end=end,
                        text=text[start:end],
                        source="dictionary",
                        source_version=entry.get("version")
                        or config["dictionary_version"],
                        source_id=entry["entry_id"],
                        tag_hint=entry["tag"],
                        priority=priorities["dictionary"],
                    )
                )

    for rule in config["rules"]:
        if not _applies(rule, config):
            continue
        pattern = re.compile(rule["pattern"])
        for match in pattern.finditer(text):
            if match.start() == match.end():
                continue
            matched_text = text[match.start() : match.end()]
            if any(char.isspace() for char in matched_text):
                continue
            candidates.append(
                _candidate(
                    start=match.start(),
                    end=match.end(),
                    text=matched_text,
                    source=rule["source"],
                    source_version=rule.get("version") or config["rule_version"],
                    source_id=rule["rule_id"],
                    tag_hint=rule["tag"],
                    priority=priorities[rule["source"]],
                )
            )

    covered = [False] * len(text)
    for candidate in candidates:
        for index in range(candidate["start"], candidate["end"]):
            covered[index] = True
    for index, char in enumerate(text):
        if covered[index]:
            continue
        candidates.append(
            _candidate(
                start=index,
                end=index + 1,
                text=char,
                source="unknown",
                source_version=None,
                source_id=None,
                tag_hint="WS" if char.isspace() else "UNK",
                priority=0,
            )
        )

    candidates.sort(
        key=lambda candidate: (
            candidate["start"],
            candidate["end"],
            candidate["source"],
            candidate["source_id"] or "",
            candidate["text"],
        )
    )
    for candidate in candidates:
        candidate["raw_start"] = normalized["char_map"][candidate["start"]][0]
        candidate["raw_end"] = normalized["char_map"][candidate["end"] - 1][1]
    return {
        "sentence_id": normalized["sentence_id"],
        "candidates": candidates,
    }


def disambiguate(
    normalized: dict[str, Any],
    candidate_artifact: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    text = normalized["normalized_text"]
    char_map = normalized["char_map"]
    by_start: dict[int, list[dict[str, Any]]] = {}
    for candidate in candidate_artifact["candidates"]:
        by_start.setdefault(candidate["start"], []).append(candidate)

    for group in by_start.values():
        group.sort(
            key=lambda candidate: (
                -float(candidate["priority"]),
                candidate["end"] - candidate["start"],
                candidate["text"],
                candidate["source"],
                candidate["source_id"] or "",
            )
        )

    tokens: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(text):
        group = by_start.get(cursor)
        if not group:
            group = [
                _candidate(
                    start=cursor,
                    end=cursor + 1,
                    text=text[cursor : cursor + 1],
                    source="unknown",
                    source_version=None,
                    source_id=None,
                    tag_hint="WS" if text[cursor].isspace() else "UNK",
                    priority=0,
                )
            ]
        selected = group[0]
        raw_start = char_map[selected["start"]][0]
        raw_end = char_map[selected["end"] - 1][1]
        tokens.append(
            {
                "text": selected["text"],
                "start": selected["start"],
                "end": selected["end"],
                "raw_start": raw_start,
                "raw_end": raw_end,
                "source": selected["source"],
                "source_version": selected["source_version"],
                "source_id": selected["source_id"],
                "tag_hint": selected["tag_hint"],
            }
        )
        cursor = selected["end"]

    return {
        "sentence_id": normalized["sentence_id"],
        "tokens": tokens,
    }

