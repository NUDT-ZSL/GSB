from __future__ import annotations

import asyncio
import sqlite3

import httpx

from .config import E_DUPLICATE, E_RENDER_TIMEOUT, TaskConfig
from .db import Database
from .extractor import extract
from .http_layer import Budgets, Fetcher, SessionManager, Stats
from .render import observe_until_ready


class CrashInterrupt(Exception):
    """Simulates a hard process kill after a checkpoint was persisted."""


async def run_task(cfg: TaskConfig, db: Database, client: httpx.AsyncClient,
                   crash_after_confirmed: int | None = None) -> dict:
    stats = Stats()
    budgets = Budgets(cfg.concurrency, cfg.retry_wait_budget)
    session = SessionManager(client, budgets, stats)
    fetcher = Fetcher(client, budgets, session, cfg, stats)

    db.create_task(cfg.task_id)
    cursor, confirmed_list = db.load_checkpoint(cfg.task_id)
    confirmed: set[str] = set(confirmed_list)
    confirmed_pairs = {(k.split("::")[0], k.split("::")[2]) for k in confirmed}
    cursor_holder: dict = {"cursor": cursor}
    page_errors: dict[str, str] = {}
    pages_processed = 0
    confirmed_this_run = 0

    async def handle_item(source: str, biz: str) -> None:
        nonlocal confirmed_this_run
        if crash_after_confirmed and confirmed_this_run >= crash_after_confirmed:
            raise CrashInterrupt(f"forced crash after {confirmed_this_run} confirmations")
        url = f"/detail/{source}/{biz}"
        outcome = await fetcher.get(url)
        if not outcome.ok:
            db.record_task_item(cfg.task_id, f"{source}::?::{biz}", source, None,
                                biz, "failed", outcome.error_class)
            return
        html = outcome.text
        result = extract(html)
        ver_label = str(result.version) if result.version is not None else "unknown"
        key = f"{source}::{ver_label}::{biz}"
        db.record_raw(cfg.task_id, url, source, ver_label, biz, outcome.status_code, html)
        if result.status == "unresolved":
            db.upsert_unresolved(cfg.task_id, source, ver_label, biz, result.raw)
            db.clear_task_item_variants(cfg.task_id, source, biz, key)
            db.record_task_item(cfg.task_id, key, source, ver_label, biz, "unresolved")
            return
        evidence = {"source": result.source, "schema_version": result.version}
        try:
            db.insert_normalized(cfg.task_id, source, result.version, biz,
                                 result.fields, evidence)
        except sqlite3.IntegrityError:
            db.clear_task_item_variants(cfg.task_id, source, biz, key)
            db.record_task_item(cfg.task_id, key, source, ver_label, biz,
                                "duplicate", E_DUPLICATE)
            return
        db.clear_task_item_variants(cfg.task_id, source, biz, key)
        db.record_task_item(cfg.task_id, key, source, ver_label, biz, "confirmed")
        confirmed.add(key)
        confirmed_pairs.add((source, biz))
        db.save_checkpoint(cfg.task_id, cursor_holder["cursor"], sorted(confirmed))
        confirmed_this_run += 1
        if crash_after_confirmed and confirmed_this_run >= crash_after_confirmed:
            raise CrashInterrupt(f"forced crash after {confirmed_this_run} confirmations")

    async def run_items(source: str, items: list) -> None:
        sem = asyncio.Semaphore(cfg.concurrency)

        async def guarded(biz: str) -> None:
            async with sem:
                await handle_item(source, biz)

        tasks = [asyncio.ensure_future(guarded(item.biz_id)) for item in items]
        if not tasks:
            return
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    source_start = cursor["source_index"] if cursor else 0
    for si in range(source_start, len(cfg.sources)):
        source = cfg.sources[si]
        page = cursor["page"] if (cursor and si == source_start) else 1
        while True:
            cursor_holder["cursor"] = {"source_index": si, "page": page}
            db.save_checkpoint(cfg.task_id, cursor_holder["cursor"], sorted(confirmed))
            list_url = f"/list/{source}?page={page}"

            async def fetch_observation() -> str:
                outcome = await fetcher.get(list_url)
                return outcome.text if outcome.ok else ""

            render = await observe_until_ready(
                fetch_observation, cfg.render_max_observations, cfg.render_interval, stats
            )
            if not render.ready:
                page_errors[list_url] = E_RENDER_TIMEOUT
                db.save_snapshot(cfg.task_id, list_url, render.observations,
                                 render.snapshot_html)
                break
            pages_processed += 1
            items = render.page.items
            if cfg.biz_filter:
                wanted = set(cfg.biz_filter)
                items = [i for i in items if i.biz_id in wanted]
            todo = [i for i in items if (source, i.biz_id) not in confirmed_pairs]
            await run_items(source, todo)
            total_pages = render.page.total_pages or page
            if render.page.next_page and page < total_pages:
                page = int(render.page.next_page)
            else:
                break

    items = db.task_items(cfg.task_id)
    failure_classes: dict[str, int] = {}
    confirmed_by_version: dict[str, int] = {}
    counts: dict[str, int] = {}
    for item in items:
        item_status = item["status"]
        counts[item_status] = counts.get(item_status, 0) + 1
        if item_status == "failed" and item["error_class"]:
            failure_classes[item["error_class"]] = failure_classes.get(item["error_class"], 0) + 1
        if item_status == "confirmed":
            ver = f"{item['source']}/{item['schema_version']}"
            confirmed_by_version[ver] = confirmed_by_version.get(ver, 0) + 1
    status = "partial" if (counts.get("failed") or page_errors) else "completed"
    summary = {
        "task_id": cfg.task_id,
        "status": status,
        "config": cfg.to_dict(),
        "total_discovered": len(items),
        "confirmed_count": counts.get("confirmed", 0),
        "failed_count": counts.get("failed", 0),
        "unresolved_count": counts.get("unresolved", 0),
        "duplicate_count": counts.get("duplicate", 0),
        "failure_classes": dict(sorted(failure_classes.items())),
        "confirmed_by_version": dict(sorted(confirmed_by_version.items())),
        "pages_processed": pages_processed,
        "page_errors": page_errors,
        "normalized_records_for_task": db.count_normalized_for_task(cfg.task_id),
        "budgets": budgets.report(),
        **stats.report(),
    }
    db.finish_task(cfg.task_id, status, summary)
    return summary
