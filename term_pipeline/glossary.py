"""Glossary loading and structural validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import E_TERM_ENTRY_INVALID, E_WEIGHT_NEGATIVE, PipelineError
from .util import read_json


def _bad(message: str, details: dict[str, Any]) -> PipelineError:
    return PipelineError(E_TERM_ENTRY_INVALID, message, details)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_glossary(config: dict[str, Any]) -> dict[str, Any]:
    path = Path(config["_resolved_paths"]["glossary"])
    try:
        raw = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise _bad("cannot read glossary", {"path": path.name, "reason": str(exc)})
    if not isinstance(raw, dict):
        raise _bad("glossary must be an object", {"path": path.name})
    version = raw.get("glossary_version")
    if not isinstance(version, str) or not version:
        raise _bad("glossary_version is required", {"path": path.name})
    if config.get("glossary_version") not in (None, version):
        raise _bad(
            "configured glossary version does not match the glossary file",
            {
                "configured": config.get("glossary_version"),
                "actual": version,
            },
        )
    entries = raw.get("entries")
    if not isinstance(entries, list) or not entries:
        raise _bad("glossary.entries must be a non-empty list", {"path": path.name})

    length_tiers = set(config["scoring"]["length_weights"])
    seen_entry_numbers: set[int] = set()
    seen_term_ids: set[str] = set()
    normalized_entries: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise _bad("glossary entry must be an object", {"entry_index": index})
        entry_no = entry.get("entry_no")
        if (
            not isinstance(entry_no, int)
            or isinstance(entry_no, bool)
            or entry_no < 1
        ):
            raise _bad(
                "glossary entry_no must be a positive integer",
                {"entry_index": index, "entry_no": entry_no},
            )
        if entry_no in seen_entry_numbers:
            raise _bad(
                "duplicate glossary entry_no",
                {"entry_index": index, "entry_no": entry_no},
            )
        seen_entry_numbers.add(entry_no)

        required_strings = ("term_id", "term", "source", "length_tier", "rule_version")
        for field in required_strings:
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                raise _bad(
                    f"glossary entry {field} must be a non-empty string",
                    {
                        "entry_index": index,
                        "entry_no": entry_no,
                        "field": field,
                        "value": value,
                    },
                )
        if entry["term_id"] in seen_term_ids:
            raise _bad(
                "duplicate glossary term_id",
                {
                    "entry_index": index,
                    "entry_no": entry_no,
                    "term_id": entry["term_id"],
                },
            )
        seen_term_ids.add(entry["term_id"])
        if any(character.isspace() for character in entry["term"]):
            raise _bad(
                "glossary term must not contain whitespace",
                {"entry_no": entry_no, "term": entry["term"]},
            )
        if entry["length_tier"] not in length_tiers:
            raise _bad(
                "glossary length_tier has no configured weight",
                {
                    "entry_no": entry_no,
                    "length_tier": entry["length_tier"],
                    "known": sorted(length_tiers),
                },
            )
        if not _number(entry.get("source_weight")):
            raise _bad(
                "glossary source_weight must be numeric",
                {
                    "entry_no": entry_no,
                    "source_weight": entry.get("source_weight"),
                },
            )
        if entry["source_weight"] < 0:
            raise PipelineError(
                E_WEIGHT_NEGATIVE,
                "glossary source_weight must not be negative",
                {
                    "entry_no": entry_no,
                    "source_weight": entry["source_weight"],
                },
            )
        senses = entry.get("senses")
        if not isinstance(senses, list) or not senses:
            raise _bad(
                "glossary senses must be a non-empty list",
                {"entry_no": entry_no},
            )
        seen_senses: set[int] = set()
        normalized_senses = []
        for sense_index, sense in enumerate(senses):
            if not isinstance(sense, dict):
                raise _bad(
                    "sense must be an object",
                    {"entry_no": entry_no, "sense_index": sense_index},
                )
            sense_id = sense.get("sense_id")
            if (
                not isinstance(sense_id, int)
                or isinstance(sense_id, bool)
                or sense_id < 1
            ):
                raise _bad(
                    "sense_id must be a positive integer",
                    {"entry_no": entry_no, "sense_id": sense_id},
                )
            if sense_id in seen_senses:
                raise _bad(
                    "duplicate sense_id in glossary entry",
                    {"entry_no": entry_no, "sense_id": sense_id},
                )
            seen_senses.add(sense_id)
            definition = sense.get("definition")
            if not isinstance(definition, str) or not definition:
                raise _bad(
                    "sense definition must be a non-empty string",
                    {"entry_no": entry_no, "sense_id": sense_id},
                )
            normalized_senses.append(
                {"sense_id": sense_id, "definition": definition}
            )
        normalized_senses.sort(key=lambda sense: sense["sense_id"])
        normalized = dict(entry)
        normalized["senses"] = normalized_senses
        normalized_entries.append(normalized)

    normalized_entries.sort(key=lambda entry: entry["entry_no"])
    return {
        "glossary_version": version,
        "entries": normalized_entries,
    }


def glossary_index(
    glossary: dict[str, Any],
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[tuple[str, int], dict[str, Any]]]:
    senses_by_term_id: dict[str, dict[int, dict[str, Any]]] = {}
    entries_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for entry in glossary["entries"]:
        senses_by_term_id[entry["term_id"]] = {
            sense["sense_id"]: sense for sense in entry["senses"]
        }
        entries_by_key[(entry["term_id"], entry["entry_no"])] = entry
    return senses_by_term_id, entries_by_key

