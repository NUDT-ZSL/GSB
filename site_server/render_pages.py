from __future__ import annotations

KNOWN_TEMPLATES = {("alpha", 1), ("alpha", 2), ("beta", 1), ("beta", 2)}


def _head(source: str, version: int) -> str:
    return (
        "<html><head>"
        f'<meta name="x-source" content="{source}">'
        f'<meta name="x-schema" content="{version}">'
        "</head>"
    )


def _alpha_v1(p: dict) -> str:
    return (
        _head("alpha", 1)
        + "<body>"
        + f'<div class="prod" data-biz="{p["business_id"]}">'
        + f'<h1 class="nm">{p["name"]}</h1>'
        + f'<span class="pr">¥{p["price_cny"]:.2f}</span>'
        + '<ul class="attrs">'
        + f'<li data-k="category">{p["category"]}</li>'
        + f'<li data-k="stock">{p["stock"]}</li>'
        + "</ul>"
        + f'<p class="desc">{p["description"]}</p>'
        + "</div></body></html>"
    )


def _alpha_v2(p: dict) -> str:
    return (
        _head("alpha", 2)
        + "<body>"
        + f'<section class="product-card" data-biz="{p["business_id"]}">'
        + f'<header><h2 class="title">{p["name"]}</h2></header>'
        + f'<data class="price" data-currency="CNY" value="{p["price_cny"]}">¥{p["price_cny"]:.2f}</data>'
        + '<dl class="specs">'
        + f"<dt>Category</dt><dd>{p['category']}</dd>"
        + f"<dt>Color</dt><dd>{p['color']}</dd>"
        + f"<dt>Stock</dt><dd>{p['stock']}</dd>"
        + "</dl>"
        + f'<p class="summary">{p["description"]}</p>'
        + "</section></body></html>"
    )


def _beta_v1(p: dict) -> str:
    return (
        _head("beta", 1)
        + "<body>"
        + f'<div class="detail" data-biz="{p["business_id"]}">'
        + '<table class="spec-table">'
        + f'<tr><th>Product</th><td>{p["name"]}</td></tr>'
        + f'<tr><th>Price</th><td>USD {p["price_usd"]:.2f}</td></tr>'
        + f'<tr><th>Dept</th><td>{p["category"]}</td></tr>'
        + f'<tr><th>Available</th><td>{p["stock"]}</td></tr>'
        + "</table>"
        + f'<div class="blurb">{p["description"]}</div>'
        + "</div></body></html>"
    )


def _beta_v2(p: dict, declared_version: int = 2) -> str:
    return (
        _head("beta", declared_version)
        + "<body>"
        + f'<div class="detail-v2" data-biz="{p["business_id"]}">'
        + f'<div data-field="name">{p["name"]}</div>'
        + '<div data-field="pricing">'
        + f'<span data-field="amount">{p["price_usd"]}</span>'
        + '<span data-field="currency">USD</span>'
        + "</div>"
        + f'<div data-field="group">{p["category"]}</div>'
        + f'<div data-field="inventory">{p["stock"]}</div>'
        + f'<div data-field="shade">{p["color"]}</div>'
        + f'<div data-field="summary">{p["description"]}</div>'
        + "</div></body></html>"
    )


def render_detail(source: str, version: int, p: dict) -> str:
    if (source, version) == ("alpha", 1):
        return _alpha_v1(p)
    if (source, version) == ("alpha", 2):
        return _alpha_v2(p)
    if (source, version) == ("beta", 1):
        return _beta_v1(p)
    if (source, version) == ("beta", 2):
        return _beta_v2(p)
    # Unknown declared versions still render a body (closest layout) so the
    # collector can exercise its unresolved path against real markup.
    return _beta_v2(p, declared_version=version)


def render_list_loading() -> str:
    return (
        "<html><body><div id=\"app\">"
        '<span class="loading">rendering</span>'
        "</div></body></html>"
    )


def render_list(source: str, items: list[str], total_pages: int, next_page: str) -> str:
    links = "".join(
        f'<a class="item" href="/detail/{source}/{b}">{b}</a>' for b in items
    )
    return (
        "<html><body>"
        f'<div id="items-container" data-total-pages="{total_pages}" '
        f'data-next-page="{next_page}">{links}</div>'
        "</body></html>"
    )
