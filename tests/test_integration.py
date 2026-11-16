"""End-to-end tests against the deterministic local site (real HTTP, in-process)."""
import asyncio
import socket

import pytest

from app.config import CrawlerConfig, SiteConfig
from app.crawler.crawler import (E_RENDER_TIMEOUT, Crawler, SimulatedCrash)
from app.crawler.parser import SHARED_FIELDS
from app.site.server import serve_in_thread
from app.storage.db import Store


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def site():
    servers = []

    def factory(cfg: SiteConfig):
        cfg.port = free_port()
        server, _ = serve_in_thread(cfg)
        servers.append(server)
        return f"http://127.0.0.1:{cfg.port}"

    yield factory
    for server in servers:
        server.should_exit = True


def make_cfg(site_url, db_path, **kw):
    cfg = CrawlerConfig(site_url=site_url, db_path=str(db_path), **kw)
    cfg.scheduler.backoff_base_ms = 5
    cfg.scheduler.backoff_max_ms = 20
    cfg.render_poll_ms = 10
    return cfg


def run(coro):
    return asyncio.run(coro)


def test_full_crawl_with_faults(site, tmp_path):
    url = site(SiteConfig(session_ttl=20, rate_limit_inflight=2,
                          fail_503={"item-003": 99}, slow_items={"item-005": 500},
                          unknown_items=["item-024"]))
    store = Store(str(tmp_path / "c.db"))
    cfg = make_cfg(url, tmp_path / "c.db")
    crawler = Crawler(store, cfg, "task-1")
    summary = run(crawler.run())

    assert summary["status"] == "done"
    assert summary["completed"] == 24
    stats = summary["stats"]
    assert stats["entries_inserted"] == 23          # item-003 never succeeds
    assert stats["unresolved"] == 1                 # item-024 unknown schemaVersion
    assert stats["failures"] == {"transient": 1}    # bounded retries exhausted
    assert stats["session_replays"] >= 1            # session expired and was replayed
    assert ("shrink", 2) in crawler.scheduler.history or \
           any(e == "shrink" for e, _ in crawler.scheduler.history)
    assert store.count_raw("task-1") > 0
    # normalized records carry version evidence
    rec = store.latest_version("item-001")
    assert rec["evidence"]["schema_version"] == 1
    assert rec["normalized"]["title"]
    rec2 = store.latest_version("item-003".replace("003", "006"))
    assert rec2["evidence"]["parser"] == "detail_v2"
    unresolved = store.latest_version("item-024")
    assert "_raw" in unresolved["normalized"]
    # expired session bodies never stored as data
    assert all(f["code"] != "E_SESSION" for f in store.list_failures("task-1"))


def test_recrawl_conflict_and_edit_flow(site, tmp_path):
    url = site(SiteConfig())
    store = Store(str(tmp_path / "e.db"))
    cfg = make_cfg(url, tmp_path / "e.db")
    run(Crawler(store, cfg, "task-a").run())
    store.store_edit("item-002", {"title": "人工修改"}, SHARED_FIELDS)
    summary = run(Crawler(store, cfg, "task-b").run())
    assert summary["stats"]["conflicts"] == 1
    assert summary["stats"]["entries_identical"] == 23
    conflicts = store.list_conflicts(status="pending")
    assert conflicts[0]["code"] == "E_EDIT_CONFLICT"
    assert store.latest_version("item-002")["normalized"]["title"] == "人工修改"


def test_render_timeout_pauses_with_snapshot(site, tmp_path):
    url = site(SiteConfig())
    store = Store(str(tmp_path / "r.db"))
    cfg = make_cfg(url, tmp_path / "r.db", render_timeout_ms=1.0)
    summary = run(Crawler(store, cfg, "task-r").run())
    assert summary["status"] == "paused"
    failures = store.list_failures("task-r")
    assert failures[0]["code"] == E_RENDER_TIMEOUT
    assert "observations" in failures[0]["detail"]
    # resumable: resume with a sane timeout completes
    cfg2 = make_cfg(url, tmp_path / "r.db")
    summary2 = run(Crawler(store, cfg2, "task-r").run())
    assert summary2["status"] == "done"
    assert summary2["completed"] == 24


def test_crash_and_resume_keeps_counts_and_revisions(site, tmp_path):
    url = site(SiteConfig())
    db = tmp_path / "k.db"
    store = Store(str(db))
    cfg = make_cfg(url, db, crash_after_items=10)
    with pytest.raises(SimulatedCrash):
        run(Crawler(store, cfg, "task-k").run())
    before = store.get_task("task-k")
    assert before["status"] == "crashed"
    assert len(before["completed_keys"]) == 16  # crash checked at page boundary
    versions_before = {r["item_key"]: [v["rev"] for v in store.versions_of(r["item_key"])]
                       for r in store.list_records()}
    raw_before = store.count_raw()

    resume_cfg = make_cfg(url, db)
    summary = run(Crawler(store, resume_cfg, "task-k").run())
    after = store.get_task("task-k")
    assert summary["status"] == "done"
    assert len(after["completed_keys"]) == 24
    # no duplicate inserts: previously confirmed items keep their revisions
    versions_after = {r["item_key"]: [v["rev"] for v in store.versions_of(r["item_key"])]
                      for r in store.list_records()}
    for key, seq in versions_before.items():
        assert versions_after[key] == seq
    for seq in versions_after.values():
        assert seq == list(range(len(seq)))
    assert store.count_raw() > raw_before  # resume fetched only the remainder


def test_session_replay_limit_pauses_task(site, tmp_path):
    url = site(SiteConfig(session_always_expired=True))
    store = Store(str(tmp_path / "s.db"))
    cfg = make_cfg(url, tmp_path / "s.db")
    cfg.scheduler.session_replay_max = 2
    summary = run(Crawler(store, cfg, "task-s").run())
    assert summary["status"] == "paused"
    codes = {f["code"] for f in store.list_failures("task-s")}
    assert "E_SESSION" in codes
