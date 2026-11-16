import pytest

from app.crawler.parser import SHARED_FIELDS
from app.storage.db import E_EDIT_CONFLICT, Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    yield s
    s.close()


def crawl_payload(title="原始标题"):
    return {"item_id": "item-001", "title": title, "category": "c",
            "price": 1, "currency": "CNY", "stock": 0, "tags": []}


def test_crawl_insert_is_idempotent(store):
    assert store.store_crawled("t", "item-001", 1, crawl_payload(), {}) == "inserted"
    assert store.store_crawled("t", "item-001", 1, crawl_payload(), {}) == "identical"
    assert [v["rev"] for v in store.versions_of("item-001")] == [0]


def test_recrawl_without_edits_updates_rev0(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    assert store.store_crawled("t", "item-001", 1, crawl_payload("新标题"), {}) == "updated"
    versions = store.versions_of("item-001")
    assert len(versions) == 1 and versions[0]["normalized"]["title"] == "新标题"


def test_edit_appends_incrementing_revision(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    rev = store.store_edit("item-001", {"title": "人工标题"}, SHARED_FIELDS)
    assert rev == 1
    rev = store.store_edit("item-001", {"stock": 5}, SHARED_FIELDS)
    assert rev == 2
    latest = store.latest_version("item-001")
    assert latest["source"] == "edit"
    assert latest["normalized"]["title"] == "人工标题"
    assert latest["normalized"]["stock"] == 5
    assert latest["evidence"]["parser"] == "manual_edit"


def test_edit_rejects_fields_outside_shared_set(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    with pytest.raises(ValueError):
        store.store_edit("item-001", {"nonexistent": 1}, SHARED_FIELDS)


def test_recrawl_with_edits_writes_conflict_not_overwrite(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    store.store_edit("item-001", {"title": "人工标题"}, SHARED_FIELDS)
    outcome = store.store_crawled("t2", "item-001", 1, crawl_payload("采集新标题"), {})
    assert outcome == "conflict"
    latest = store.latest_version("item-001")
    assert latest["rev"] == 1 and latest["normalized"]["title"] == "人工标题"
    conflicts = store.list_conflicts(status="pending")
    assert len(conflicts) == 1
    assert conflicts[0]["code"] == E_EDIT_CONFLICT
    assert conflicts[0]["existing_rev"] == 1
    assert "采集新标题" in conflicts[0]["crawl_normalized_json"]


def test_recrawl_matching_edit_is_identical(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    store.store_edit("item-001", {"title": "人工标题"}, SHARED_FIELDS)
    edited = store.latest_version("item-001")["normalized"]
    assert store.store_crawled("t2", "item-001", 1, edited, {}) == "identical"


def test_resolve_conflict_accept_crawl_appends_revision(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    store.store_edit("item-001", {"title": "人工标题"}, SHARED_FIELDS)
    store.store_crawled("t2", "item-001", 1, crawl_payload("采集新标题"), {})
    conflict = store.list_conflicts(status="pending")[0]
    store.resolve_conflict(conflict["id"], "accept_crawl")
    latest = store.latest_version("item-001")
    assert latest["rev"] == 2 and latest["normalized"]["title"] == "采集新标题"
    assert store.list_conflicts(status="pending") == []


def test_resolve_conflict_keep_edit(store):
    store.store_crawled("t", "item-001", 1, crawl_payload(), {})
    store.store_edit("item-001", {"title": "人工标题"}, SHARED_FIELDS)
    store.store_crawled("t2", "item-001", 1, crawl_payload("采集新标题"), {})
    conflict = store.list_conflicts(status="pending")[0]
    store.resolve_conflict(conflict["id"], "keep_edit")
    assert store.latest_version("item-001")["normalized"]["title"] == "人工标题"
