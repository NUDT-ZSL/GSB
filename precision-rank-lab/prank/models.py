"""Plain data containers for queries and candidate items."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Query:
    query_id: str
    query_text: str
    query_terms: tuple[str, ...]


@dataclass(frozen=True)
class Item:
    item_id: str
    item_terms: tuple[str, ...]
    category: str
    price: float
    quality: float


@dataclass
class CandidateSet:
    query: Query
    items: list[Item] = field(default_factory=list)
