from __future__ import annotations

import asyncio
import traceback
import uuid
from typing import Any

from app.crawler import Crawler
from app.db import Database


class TaskManager:
    def __init__(self, db: Database, archive_base_url: str):
        self.db = db
        self.archive_base_url = archive_base_url.rstrip("/")
        self._running: dict[str, asyncio.Task[None]] = {}

    def is_running(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        return bool(task and not task.done())

    def launch(self, task_id: str, *, resuming: bool = False) -> bool:
        if self.is_running(task_id):
            return False
        if not self.db.begin_task_run(task_id, resuming=resuming):
            return False
        if resuming:
            self.db.mark_interrupted_sessions(task_id)
            self.db.reset_failed_items(task_id)
        background = asyncio.create_task(self._execute(task_id), name=f"crawl-{task_id}")
        self._running[task_id] = background
        background.add_done_callback(lambda _task, tid=task_id: self._running.pop(tid, None))
        return True

    async def _execute(self, task_id: str) -> None:
        task = self.db.get_task(task_id)
        if not task:
            return
        try:
            crawler = Crawler(
                db=self.db,
                task=task,
                archive_base_url=task["config"].get("_archive_base_url", self.archive_base_url),
            )
            await crawler.run()
        except asyncio.CancelledError:
            # A graceful cancellation leaves the durable status as running so the
            # explicit resume endpoint can continue from the checkpoint.
            raise
        except Exception as exc:
            summary = self.db.task_summary(task_id)
            summary["task_status"] = "failed"
            summary["task_code"] = "E_TASK"
            summary["task_failure_count"] = 1
            summary["recoverable"] = True
            self.db.finish_task(
                task_id,
                "failed",
                "E_TASK",
                summary,
                error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}",
            )

    async def shutdown(self) -> None:
        active = [task for task in self._running.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


def create_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:16]}"
