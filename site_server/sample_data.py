from __future__ import annotations

SOURCES = ("alpha", "beta")
ITEM_COUNT = 6
PAGE_SIZE = 3
UNKNOWN_SCHEMA_VERSION = 9


def biz_id(n: int) -> str:
    return f"BIZ-{n:03d}"


def all_biz_ids() -> list[str]:
    return [biz_id(n) for n in range(1, ITEM_COUNT + 1)]


def default_version(source: str, n: int) -> int:
    if source == "alpha":
        return 1 if n <= 3 else 2
    if n <= 2:
        return 1
    if n <= 5:
        return 2
    return UNKNOWN_SCHEMA_VERSION


def page_slice(page: int) -> list[str]:
    ids = all_biz_ids()
    start = (page - 1) * PAGE_SIZE
    return ids[start:start + PAGE_SIZE]


def total_pages() -> int:
    return (ITEM_COUNT + PAGE_SIZE - 1) // PAGE_SIZE


def product(biz: str) -> dict:
    n = int(biz.rsplit("-", 1)[1])
    return {
        "business_id": biz,
        "name": f"Gadget-{n:03d}",
        "price_cny": round(80.0 + n * 7.5, 2),
        "price_usd": round(10.0 + n * 1.25, 2),
        "category": f"cat-{n % 3}",
        "stock": 5 + n,
        "color": ["red", "blue", "green", "black", "white", "gray"][(n - 1) % 6],
        "description": f"Deterministic description for {biz}.",
    }
