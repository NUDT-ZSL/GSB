"""Detail page parsing with schemaVersion dispatch.

The normalized field set (SHARED_FIELDS) is the single contract shared by the
crawler and the editing console.  Unknown schema versions degrade to keeping
the raw fields under `_raw` flagged unresolved - data is never dropped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SHARED_FIELDS = ("item_id", "title", "category", "price", "currency", "stock", "tags")

_META_RE = re.compile(r'<meta\s+name="schemaVersion"\s+content="(\d+)"')
_DATA_RE = re.compile(r'<script\s+type="application/json"\s+id="detail-data">(.*?)</script>',
                      re.DOTALL)


class ParseFailure(Exception):
    """Item-level parse failure (category 'parse', code E_PARSE)."""


@dataclass
class ParseResult:
    schema_version: int
    normalized: dict
    evidence: dict = field(default_factory=dict)
    unresolved: bool = False


def normalize(schema_version: int, raw: dict) -> tuple[dict, str]:
    """Map a raw version-specific payload onto the shared field set."""
    if schema_version == 1:
        return {
            "item_id": raw["id"],
            "title": raw["title"],
            "category": raw["category"],
            "price": raw["price"],
            "currency": "CNY",
            "stock": raw["stock"],
            "tags": [],
        }, "detail_v1"
    if schema_version == 2:
        return {
            "item_id": raw["id"],
            "title": raw["name"],
            "category": raw["category"]["label"],
            "price": raw["pricing"]["amount"],
            "currency": raw["pricing"]["currency"],
            "stock": raw["stock"],
            "tags": list(raw.get("tags", [])),
        }, "detail_v2"
    # unknown version: keep everything, mark unresolved
    return {"_raw": dict(raw)}, "unresolved"


def parse_detail(html: str) -> ParseResult:
    meta = _META_RE.search(html)
    data = _DATA_RE.search(html)
    if not meta or not data:
        raise ParseFailure("detail page missing schemaVersion meta or detail-data script")
    schema_version = int(meta.group(1))
    try:
        raw = json.loads(data.group(1))
    except json.JSONDecodeError as exc:
        raise ParseFailure(f"detail-data is not valid JSON: {exc}") from exc
    normalized, parser_name = normalize(schema_version, raw)
    evidence = {
        "schema_version": schema_version,
        "parser": parser_name,
        "source_fields": sorted(raw.keys()),
    }
    return ParseResult(
        schema_version=schema_version,
        normalized=normalized,
        evidence=evidence,
        unresolved=(parser_name == "unresolved"),
    )


def parse_list_meta(html: str) -> dict:
    """Extract render source, total pages and next-page cursor from a list page."""
    m = re.search(
        r'<div\s+id="items"\s+data-render-src="([^"]+)"\s+data-total-pages="(\d+)"'
        r'\s+data-next-page="([^"]*)"', html)
    if not m:
        raise ParseFailure("list page missing #items container declaration")
    return {
        "render_src": m.group(1),
        "total_pages": int(m.group(2)),
        "next_page": int(m.group(3)) if m.group(3) else None,
    }
