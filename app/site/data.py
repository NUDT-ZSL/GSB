"""Deterministic sample dataset for the local target site.

Item ids are item-001 .. item-(pages*per_page).  Every third item defaults to
schemaVersion 2, the rest to schemaVersion 1; config can override per item or
mark items as unknown-version samples.
"""
from __future__ import annotations

from ..config import SiteConfig

UNKNOWN_SCHEMA_VERSION = 99


def build_dataset(cfg: SiteConfig) -> dict:
    dataset = {}
    total = cfg.pages * cfg.per_page
    for idx in range(1, total + 1):
        item_id = f"item-{idx:03d}"
        if item_id in cfg.unknown_items:
            version = UNKNOWN_SCHEMA_VERSION
        else:
            version = cfg.version_overrides.get(item_id, 2 if idx % 3 == 0 else 1)
        dataset[item_id] = {
            "item_id": item_id,
            "schemaVersion": version,
            "payload": _payload(item_id, idx, version),
        }
    return dataset


def _payload(item_id: str, idx: int, version: int) -> dict:
    if version == 1:
        return {
            "id": item_id,
            "title": f"资料条目 {idx:03d}",
            "category": f"类目-{idx % 4}",
            "price": 100 + idx,
            "stock": idx % 7,
        }
    if version == 2:
        return {
            "id": item_id,
            "name": f"资料条目 {idx:03d}",
            "category": {"id": idx % 4, "label": f"类目-{idx % 4}"},
            "pricing": {"amount": 100 + idx, "currency": "CNY"},
            "tags": [f"tag-{idx % 3}", "v2"],
            "stock": idx % 7,
        }
    # Unknown schema version: deliberately different field names.
    return {
        "uid": item_id,
        "headline": f"资料条目 {idx:03d}",
        "group_name": f"类目-{idx % 4}",
        "cost_fen": (100 + idx) * 100,
        "remain": idx % 7,
    }


def page_items(cfg: SiteConfig, page: int) -> list:
    start = (page - 1) * cfg.per_page + 1
    end = min(start + cfg.per_page - 1, cfg.pages * cfg.per_page)
    return [f"item-{i:03d}" for i in range(start, end + 1)]
