from __future__ import annotations

from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

CANONICAL_FIELDS = [
    "business_id",
    "name",
    "price",
    "currency",
    "category",
    "stock",
    "color",
    "description",
]
REQUIRED_FIELDS = ["business_id", "name", "price", "currency", "category"]


def parse_declaration(html: str) -> tuple[str | None, int | None]:
    tree = HTMLParser(html)
    source_node = tree.css_first('meta[name="x-source"]')
    version_node = tree.css_first('meta[name="x-schema"]')
    source = source_node.attributes.get("content") if source_node else None
    version = None
    if version_node is not None:
        try:
            version = int(version_node.attributes.get("content", ""))
        except (TypeError, ValueError):
            version = None
    return source, version


def _text(node) -> str | None:
    return node.text(strip=True) if node is not None else None


def _parse_price(text: str) -> tuple[float, str]:
    text = text.strip()
    if text.startswith("¥"):
        return float(text[1:]), "CNY"
    if text.upper().startswith("USD"):
        return float(text[3:].strip()), "USD"
    return float(text), ""


def _alpha_v1(tree: HTMLParser) -> dict:
    root = tree.css_first("div.prod")
    price, currency = _parse_price(_text(root.css_first("span.pr")))
    attrs = {
        li.attributes.get("data-k"): li.text(strip=True)
        for li in root.css("ul.attrs li")
    }
    return {
        "business_id": root.attributes.get("data-biz"),
        "name": _text(root.css_first("h1.nm")),
        "price": price,
        "currency": currency,
        "category": attrs.get("category"),
        "stock": int(attrs.get("stock", "0")),
        "description": _text(root.css_first("p.desc")),
    }


def _alpha_v2(tree: HTMLParser) -> dict:
    root = tree.css_first("section.product-card")
    price_node = root.css_first("data.price")
    specs = {}
    dts = root.css("dl.specs dt")
    dds = root.css("dl.specs dd")
    for dt, dd in zip(dts, dds):
        specs[dt.text(strip=True).lower()] = dd.text(strip=True)
    return {
        "business_id": root.attributes.get("data-biz"),
        "name": _text(root.css_first("h2.title")),
        "price": float(price_node.attributes.get("value")),
        "currency": price_node.attributes.get("data-currency"),
        "category": specs.get("category"),
        "stock": int(specs.get("stock", "0")),
        "color": specs.get("color"),
        "description": _text(root.css_first("p.summary")),
    }


def _beta_v1(tree: HTMLParser) -> dict:
    root = tree.css_first("div.detail")
    rows = {}
    for tr in root.css("table.spec-table tr"):
        th = tr.css_first("th")
        td = tr.css_first("td")
        if th is not None and td is not None:
            rows[th.text(strip=True).lower()] = td.text(strip=True)
    price, currency = _parse_price(rows.get("price", "0"))
    return {
        "business_id": root.attributes.get("data-biz"),
        "name": rows.get("product"),
        "price": price,
        "currency": currency,
        "category": rows.get("dept"),
        "stock": int(rows.get("available", "0")),
        "description": _text(root.css_first("div.blurb")),
    }


def _beta_v2(tree: HTMLParser) -> dict:
    root = tree.css_first("div.detail-v2")

    def field(name: str) -> str | None:
        return _text(root.css_first(f'[data-field="{name}"]'))

    return {
        "business_id": root.attributes.get("data-biz"),
        "name": field("name"),
        "price": float(field("amount")),
        "currency": field("currency"),
        "category": field("group"),
        "stock": int(field("inventory")),
        "color": field("shade"),
        "description": field("summary"),
    }


RULES = {
    ("alpha", 1): _alpha_v1,
    ("alpha", 2): _alpha_v2,
    ("beta", 1): _beta_v1,
    ("beta", 2): _beta_v2,
}


@dataclass
class Extraction:
    status: str  # "ok" | "unresolved"
    source: str | None
    version: int | None
    fields: dict | None
    raw: dict = field(default_factory=dict)
    reason: str | None = None


def extract(html: str) -> Extraction:
    source, version = parse_declaration(html)
    tree = HTMLParser(html)
    body = tree.body
    excerpt = body.text(separator=" ", strip=True)[:200] if body is not None else ""
    raw = {
        "declared_source": source,
        "declared_version": version,
        "body_excerpt": excerpt,
    }
    if source is None or version is None:
        return Extraction("unresolved", source, version, None, raw, "missing_declaration")
    rule = RULES.get((source, version))
    if rule is None:
        return Extraction("unresolved", source, version, None, raw, "unknown_schema_version")
    return Extraction("ok", source, version, rule(tree), raw, None)
