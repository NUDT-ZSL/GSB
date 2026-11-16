from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser


@dataclass
class ListItem:
    biz_id: str
    url: str


@dataclass
class PageModel:
    container_present: bool
    item_count: int
    total_pages: int | None
    next_page: str
    items: list[ListItem] = field(default_factory=list)


def parse_list_page(html: str) -> PageModel:
    tree = HTMLParser(html)
    container = tree.css_first("#items-container")
    if container is None:
        return PageModel(False, 0, None, "", [])
    total_raw = container.attributes.get("data-total-pages", "")
    try:
        total_pages = int(total_raw)
    except (TypeError, ValueError):
        total_pages = None
    next_page = container.attributes.get("data-next-page", "") or ""
    items: list[ListItem] = []
    for anchor in container.css("a.item"):
        href = anchor.attributes.get("href", "")
        parts = [p for p in href.split("/") if p]
        if len(parts) >= 3 and parts[0] == "detail":
            items.append(ListItem(biz_id=parts[2], url=href))
    return PageModel(True, len(items), total_pages, next_page, items)


@dataclass
class RenderResult:
    ready: bool
    observations: int
    page: PageModel | None
    snapshot_html: str


async def observe_until_ready(fetch, max_observations: int, interval: float, stats=None) -> RenderResult:
    """Poll the list page until the item container is present and the item
    count is stable across two consecutive observations. Never relies on a
    fixed sleep as a readiness signal."""
    previous_count: int | None = None
    last_html = ""
    last_model: PageModel | None = None
    for observation in range(1, max_observations + 1):
        last_html = await fetch()
        if stats is not None:
            stats.render_observations += 1
        model = parse_list_page(last_html)
        last_model = model
        if (
            model.container_present
            and previous_count is not None
            and model.item_count == previous_count
        ):
            return RenderResult(True, observation, model, last_html)
        previous_count = model.item_count if model.container_present else None
        if observation < max_observations:
            await asyncio.sleep(interval)
    return RenderResult(False, max_observations, last_model, last_html)
