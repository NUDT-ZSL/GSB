"""Deterministic end-to-end demo.

Runs the local site (with injected 503 / slow responses / session expiry /
rate limiting), a full crawl, a manual edit, a conflicting re-crawl and a
kill-and-resume cycle, then prints a deterministic summary.  Repeating the
script yields identical entry counts, failure categories and revision summary.

Usage: .venv/bin/python demo.py
"""
import asyncio
import json
from pathlib import Path

from app.config import CrawlerConfig, SiteConfig
from app.crawler.crawler import Crawler, SimulatedCrash
from app.crawler.parser import SHARED_FIELDS
from app.site.server import serve_in_thread
from app.storage.db import Store

DB_PATH = "artifacts/demo.db"
SITE_PORT = 8711


def revision_summary(store: Store) -> dict:
    return {r["item_key"]: [v["rev"] for v in store.versions_of(r["item_key"])]
            for r in store.list_records()}


def revision_sequences_ok(store: Store) -> bool:
    return all(seq == list(range(len(seq))) for seq in revision_summary(store).values())


async def main():
    Path(DB_PATH).unlink(missing_ok=True)
    site_cfg = SiteConfig(
        port=SITE_PORT,
        session_ttl=30,                 # sessions expire after 30 authenticated requests
        rate_limit_inflight=2,          # 429 when more than 2 requests in flight
        fail_503={"item-003": 99},      # item-003 keeps failing: item-level failure
        slow_items={"item-005": 600},   # latency injection
        unknown_items=["item-024"],     # unknown schemaVersion sample
    )
    server, _thread = serve_in_thread(site_cfg)
    site_url = f"http://127.0.0.1:{SITE_PORT}"
    store = Store(DB_PATH)

    cfg = CrawlerConfig(site_url=site_url, db_path=DB_PATH)
    cfg.scheduler.initial_inflight = 4
    cfg.scheduler.max_inflight = 6

    # 1. full crawl with fault injection
    crawl_main = Crawler(store, cfg, "task-main")
    summary_main = await crawl_main.run()
    backpressure_events = list(crawl_main.scheduler.history)

    # 2. manual edit -> new revision
    edit_rev = store.store_edit("item-001", {"title": "人工修订标题", "price": 999},
                                SHARED_FIELDS, editor="demo")

    # 3. re-crawl: manual edit must not be overwritten -> E_EDIT_CONFLICT
    summary_recrawl = await Crawler(store, cfg, "task-recrawl").run()

    # 4. kill-and-resume: hard crash after a page boundary, resume from SQLite only
    crash_cfg = CrawlerConfig(site_url=site_url, db_path=DB_PATH, crash_after_items=10)
    try:
        await Crawler(store, crash_cfg, "task-crash").run()
    except SimulatedCrash as exc:
        print(f"simulated crash: {exc}")
    before = store.get_task("task-crash")
    resume_cfg = CrawlerConfig(site_url=site_url, db_path=DB_PATH)
    summary_resumed = await Crawler(store, resume_cfg, "task-crash").run()
    after = store.get_task("task-crash")

    conflicts = store.list_conflicts(status="pending")
    deterministic = {
        "task_main": {
            "status": summary_main["status"],
            "completed": summary_main["completed"],
            "entries_inserted": summary_main["stats"]["entries_inserted"],
            "unresolved": summary_main["stats"]["unresolved"],
            "failures": summary_main["stats"]["failures"],
        },
        "edit": {"item_key": "item-001", "new_rev": edit_rev},
        "task_recrawl": {
            "status": summary_recrawl["status"],
            "entries_identical": summary_recrawl["stats"]["entries_identical"],
            "conflicts": summary_recrawl["stats"]["conflicts"],
            "failures": summary_recrawl["stats"]["failures"],
        },
        "conflict_codes": sorted({c["code"] for c in conflicts}),
        "task_crash_resume": {
            "status_before": before["status"],
            "completed_before": len(before["completed_keys"]),
            "status_after": after["status"],
            "completed_after": len(after["completed_keys"]),
            "failures": summary_resumed["stats"]["failures"],
        },
        "revision_sequences_ok": revision_sequences_ok(store),
        "total_versions": sum(len(s) for s in revision_summary(store).values()),
    }
    informational = {
        "backpressure_events": backpressure_events,
        "retries": {t: store.get_task(t)["stats"]["retries"]
                    for t in ("task-main", "task-recrawl", "task-crash")},
        "session_replays": {t: store.get_task(t)["stats"]["session_replays"]
                            for t in ("task-main", "task-recrawl", "task-crash")},
        "raw_responses": store.count_raw(),
    }
    print("=== DETERMINISTIC SUMMARY ===")
    print(json.dumps(deterministic, ensure_ascii=False, indent=2, sort_keys=True))
    print("=== INFORMATIONAL (timing dependent) ===")
    print(json.dumps(informational, ensure_ascii=False, indent=2, sort_keys=True))
    server.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())
