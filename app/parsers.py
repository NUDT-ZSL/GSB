"""详情页解析：按页面声明的 schema 版本选择规则；未知版本降级为原始字段保留。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_META_RE = re.compile(r'<meta[^>]+name="schema-version"[^>]+content="([^"]+)"', re.I)


@dataclass
class ParseResult:
    version: str
    parse_status: str          # "ok" | "raw_fallback"
    raw: dict
    normalized: dict | None


class _DlParser(HTMLParser):
    """解析 <dl><dt>key</dt><dd>value</dd></dl> 结构（schema v1）。"""

    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}
        self._tag = None
        self._key = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("dt", "dd"):
            self._tag = tag
            self._buf = []

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "dt" and self._tag == "dt":
            self._key = "".join(self._buf).strip()
            self._tag = None
        elif tag == "dd" and self._tag == "dd":
            if self._key:
                self.fields[self._key] = "".join(self._buf).strip()
            self._tag = None


class _DataFieldParser(HTMLParser):
    """解析 <span data-field="key">value</span> 结构（schema v2）。"""

    def __init__(self, attr: str = "data-field"):
        super().__init__()
        self.attr = attr
        self.fields: dict[str, str] = {}
        self._key = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if self.attr in d:
            self._key = d[self.attr]
            self._buf = []

    def handle_data(self, data):
        if self._key:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._key:
            self.fields[self._key] = "".join(self._buf).strip()
            self._key = None


def _declared_version(html: str) -> str | None:
    m = _META_RE.search(html)
    return m.group(1) if m else None


def _parse_v1(html: str) -> dict:
    p = _DlParser()
    p.feed(html)
    return p.fields


def _parse_v2(html: str) -> dict:
    p = _DataFieldParser("data-field")
    p.feed(html)
    return p.fields


def _normalize_v1(raw: dict) -> dict:
    return {
        "ticket_id": raw.get("ticket_id"),
        "title": raw.get("title"),
        "assignee": raw.get("requester"),
        "priority": raw.get("priority"),
        "created_at": raw.get("created_at"),
        "description": raw.get("description"),
        "schema_version": "v1",
        "fields": raw,
    }


def _normalize_v2(raw: dict) -> dict:
    return {
        "ticket_id": raw.get("ticket_id"),
        "title": raw.get("title"),
        "assignee": raw.get("assignee"),
        "department": raw.get("department"),
        "category": raw.get("category"),
        "due_date": raw.get("due_date"),
        "priority": raw.get("priority"),
        "created_at": raw.get("created_at"),
        "description": raw.get("description"),
        "schema_version": "v2",
        "fields": raw,
    }


def parse_detail(html: str, ticket_id: str) -> ParseResult:
    version = _declared_version(html) or "unknown"
    if version == "v1":
        raw = _parse_v1(html)
        return ParseResult(version, "ok", raw, _normalize_v1(raw))
    if version == "v2":
        raw = _parse_v2(html)
        return ParseResult(version, "ok", raw, _normalize_v2(raw))
    # 未知版本：尽力提取已知结构的字段，并保留截断的原始 HTML，不静默丢弃
    raw = _parse_v2(html) or _parse_v1(html)
    raw["_html_excerpt"] = html[:8000]
    return ParseResult(version, "raw_fallback", raw, None)
