"""Normalization facade."""

from __future__ import annotations

from .mappings import MappingTable


def normalize_query(text: str, mappings: MappingTable) -> str:
    return mappings.normalize(text)

