"""Versioned domain dictionary with deterministic longest matching."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_DICT_TRUNCATED, E_INPUT_INVALID, QUError
from .util import payload_checksum, read_json


@dataclass(frozen=True)
class DictionaryEntry:
    entry_id: str
    surface: str
    canonical_value: str
    kind: str
    domain: str
    intent_class: str
    slot: str | None
    weight: float

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "DictionaryEntry":
        required = (
            "entry_id",
            "surface",
            "canonical_value",
            "kind",
            "domain",
            "intent_class",
            "slot",
            "weight",
        )
        for key in required:
            if key not in row:
                raise QUError(
                    E_INPUT_INVALID,
                    f"dictionary entry is missing field {key}",
                    stage="tokenize",
                    details={"row": row},
                )
        surface = str(row["surface"])
        if not surface or surface != surface.strip():
            raise QUError(
                E_INPUT_INVALID,
                "dictionary surface must be non-empty and trimmed",
                stage="tokenize",
                details={"entry_id": row["entry_id"], "surface": surface},
            )
        return cls(
            entry_id=str(row["entry_id"]),
            surface=surface,
            canonical_value=str(row["canonical_value"]),
            kind=str(row["kind"]),
            domain=str(row["domain"]),
            intent_class=str(row["intent_class"]),
            slot=None if row["slot"] is None else str(row["slot"]),
            weight=float(row["weight"]),
        )


@dataclass
class Dictionary:
    version: int
    entries: tuple[DictionaryEntry, ...]
    checksum: str
    by_id: dict[str, DictionaryEntry]
    by_surface: dict[str, tuple[str, ...]]
    trie: dict[str, Any]

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        max_entries: int | None = None,
        allow_truncate: bool = False,
    ) -> "Dictionary":
        if "version" not in payload or "entries" not in payload:
            raise QUError(
                E_INPUT_INVALID,
                "dictionary payload must contain version and entries",
                stage="tokenize",
            )
        version = int(payload["version"])
        rows = payload["entries"]
        if not isinstance(rows, list):
            raise QUError(
                E_INPUT_INVALID,
                "dictionary entries must be a list",
                stage="tokenize",
            )
        original_count = len(rows)
        selected = rows
        if max_entries is not None and original_count > max_entries:
            if not allow_truncate:
                raise QUError(
                    E_DICT_TRUNCATED,
                    "dictionary entry count exceeds configured maximum",
                    stage="tokenize",
                    category="max_entries",
                    details={
                        "entry_count": original_count,
                        "max_entries": max_entries,
                    },
                )
            selected = rows[:max_entries]
        entries = tuple(DictionaryEntry.from_dict(row) for row in selected)
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise QUError(
                E_INPUT_INVALID,
                "dictionary entry_id values must be unique",
                stage="tokenize",
            )
        by_id = {entry.entry_id: entry for entry in entries}
        surface_lists: dict[str, list[str]] = {}
        for entry in entries:
            surface_lists.setdefault(entry.surface, []).append(entry.entry_id)
        by_surface = {
            surface: tuple(sorted(ids)) for surface, ids in sorted(surface_lists.items())
        }

        trie: dict[str, Any] = {}
        for surface, entry_ids in by_surface.items():
            node = trie
            for char in surface:
                node = node.setdefault(char, {})
            node["#ids"] = entry_ids

        expected = payload.get("checksum")
        actual = payload_checksum(payload)
        if expected is not None and expected != actual:
            raise QUError(
                E_INPUT_INVALID,
                "dictionary checksum mismatch",
                stage="tokenize",
                details={"expected": expected, "actual": actual},
            )
        return cls(
            version=version,
            entries=entries,
            checksum=actual,
            by_id=by_id,
            by_surface=by_surface,
            trie=trie,
        )

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        max_entries: int | None = None,
        allow_truncate: bool = False,
    ) -> "Dictionary":
        return cls.from_payload(
            read_json(path),
            max_entries=max_entries,
            allow_truncate=allow_truncate,
        )

    def longest_match(self, text: str, start: int) -> tuple[int, tuple[str, ...]] | None:
        node = self.trie
        best: tuple[int, tuple[str, ...]] | None = None
        cursor = start
        while cursor < len(text):
            char = text[cursor]
            if char not in node:
                break
            node = node[char]
            cursor += 1
            entry_ids = node.get("#ids")
            if entry_ids:
                best = (cursor, entry_ids)
        return best

    def entry(self, entry_id: str) -> DictionaryEntry:
        return self.by_id[entry_id]

