from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.db import canonical_json


class RenderContractError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class ListContract:
    render_api: str
    container_selector: str
    item_selector: str
    stable_observations: int
    declaration: str


@dataclass
class ListEntry:
    business_id: str
    schema_version: str
    revision: int
    detail_url: str
    item_key: str


@dataclass
class RenderObservation:
    container_present: bool
    item_count: int
    stable_count: int
    page: int | None
    total_pages: int | None
    next_page: int | None
    items: list[ListEntry] = field(default_factory=list)
    body_sha256: str = ""
    fragment_excerpt: str = ""


def parse_list_contract(page_html: str) -> ListContract:
    soup = BeautifulSoup(page_html, "lxml")
    node = soup.find("script", id="render-contract")
    if node is not None and node.string:
        try:
            raw = json.loads(node.string)
        except json.JSONDecodeError as exc:
            raise RenderContractError("E_RENDER_CONTRACT", f"invalid render contract JSON: {exc}")
        render_api = raw.get("render_api")
        if not render_api:
            raise RenderContractError("E_RENDER_CONTRACT", "render contract has no render_api")
        return ListContract(
            render_api=str(render_api),
            container_selector=str(raw.get("container") or "#entry-container"),
            item_selector=str(raw.get("item_selector") or "[data-entry]"),
            stable_observations=max(1, int(raw.get("stable_observations") or 2)),
            declaration=str(raw.get("declaration") or ""),
        )
    app = soup.find(id="archive-app")
    if app is None or not app.get("data-render-api"):
        raise RenderContractError("E_RENDER_CONTRACT", "list page has no secondary render contract")
    return ListContract(
        render_api=str(app["data-render-api"]),
        container_selector="#entry-container",
        item_selector="[data-entry]",
        stable_observations=2,
        declaration="container-present-and-count-stable-twice",
    )


def _clean_optional_int(value: str | None) -> int | None:
    if value is None or str(value).strip() in {"", "None", "null"}:
        return None
    return int(value)


def _item_key(business_id: str, schema_version: str, revision: int, detail_url: str) -> str:
    parts = urlsplit(detail_url)
    stable_url = f"{parts.path}?{parts.query}" if parts.query else parts.path
    digest = hashlib.sha256(
        canonical_json(
            {
                "business_id": business_id,
                "schema_version": schema_version,
                "revision": revision,
                "detail_url": stable_url,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"item:{digest}"


def parse_fragment(fragment_html: str, contract: ListContract) -> RenderObservation:
    soup = BeautifulSoup(fragment_html, "lxml")
    container = soup.select_one(contract.container_selector)
    if container is None:
        return RenderObservation(
            container_present=False,
            item_count=0,
            stable_count=0,
            page=None,
            total_pages=None,
            next_page=None,
            body_sha256=hashlib.sha256(fragment_html.encode("utf-8")).hexdigest(),
            fragment_excerpt=fragment_html[:1000],
        )

    entry_nodes = container.select(contract.item_selector)
    items: list[ListEntry] = []
    for node in entry_nodes:
        business_id = (node.get("data-business-id") or "").strip()
        schema_version = (node.get("data-schema-version") or "").strip() or "__missing__"
        revision_raw = (node.get("data-revision") or "").strip()
        detail_url = (node.get("data-detail-url") or "").strip()
        if not detail_url:
            anchor = node.find("a", href=True)
            detail_url = anchor["href"] if anchor else ""
        if not business_id or not detail_url:
            raise RenderContractError(
                "E_RENDER_CONTRACT",
                "entry is missing data-business-id or detail URL",
                {"entry_html": str(node)[:500]},
            )
        try:
            revision = int(revision_raw)
        except ValueError as exc:
            raise RenderContractError(
                "E_RENDER_CONTRACT",
                f"entry revision is not an integer: {revision_raw!r}",
                {"business_id": business_id},
            ) from exc
        items.append(
            ListEntry(
                business_id=business_id,
                schema_version=schema_version,
                revision=revision,
                detail_url=detail_url,
                item_key=_item_key(business_id, schema_version, revision, detail_url),
            )
        )

    try:
        page = _clean_optional_int(container.get("data-page"))
        total_pages = _clean_optional_int(container.get("data-total-pages"))
        next_page = _clean_optional_int(container.get("data-next-page"))
    except ValueError as exc:
        raise RenderContractError("E_CURSOR_INVALID", f"non-integer pagination declaration: {exc}")

    return RenderObservation(
        container_present=True,
        item_count=len(items),
        stable_count=0,
        page=page,
        total_pages=total_pages,
        next_page=next_page,
        items=items,
        body_sha256=hashlib.sha256(fragment_html.encode("utf-8")).hexdigest(),
        fragment_excerpt=fragment_html[:1000],
    )


def validate_cursor(observation: RenderObservation, requested_page: int) -> None:
    if not observation.container_present:
        raise RenderContractError("E_CURSOR_INVALID", "entry container is absent")
    if observation.page != requested_page:
        raise RenderContractError(
            "E_CURSOR_INVALID",
            f"declared page {observation.page!r} does not match requested page {requested_page}",
        )
    if observation.total_pages is None or observation.total_pages < 1:
        raise RenderContractError("E_CURSOR_INVALID", "total page declaration is missing or invalid")
    if requested_page > observation.total_pages:
        raise RenderContractError("E_CURSOR_INVALID", "requested page exceeds declared total pages")
    if requested_page < observation.total_pages:
        if observation.next_page is None:
            raise RenderContractError("E_CURSOR_INVALID", "next-page marker is missing before the last page")
        if observation.next_page != requested_page + 1:
            raise RenderContractError(
                "E_CURSOR_INVALID",
                f"next-page marker {observation.next_page} does not advance by exactly one",
            )
    elif observation.next_page is not None:
        raise RenderContractError(
            "E_CURSOR_INVALID",
            "last page must not declare a next-page marker",
        )
