from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .errors import fail
from .fslock import read_json
from .planner import plan_hash, plan_windows
from .storage import Store
from .validation import Dataset, validate_dataset


class SchedulerService:
    def __init__(
        self,
        db_path: str | Path,
        lock_path: str | Path,
        tmp_dir: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)
        self.tmp_dir = Path(tmp_dir) if tmp_dir is not None else Path(".tmp")
        self.store = Store(self.db_path, self.lock_path)

    def load_files(
        self,
        model_path: str | Path,
        replicas_path: str | Path,
        requests_path: str | Path,
        precision_path: str | Path,
    ) -> dict[str, Any]:
        dataset = validate_dataset(
            read_json(model_path),
            read_json(replicas_path),
            read_json(requests_path),
            read_json(precision_path),
        )
        return self.load_dataset(dataset)

    def load_dataset(self, dataset: Dataset) -> dict[str, Any]:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        return self.store.load(dataset)

    def _crash(self, point: str, requested: str | None) -> None:
        selected = requested or os.environ.get("SCHED_CRASH_AT")
        if selected and point == selected:
            os._exit(137)

    def _commit_prepared(self, crash_at: str | None) -> dict[str, int]:
        prepared = self.store.prepared_windows()
        committed = 0
        already = 0
        crash_checked = False
        for plan in prepared:
            status = self.store.commit_window(
                plan,
                crash_hook=lambda point: self._crash(point, crash_at),
            )
            if status == "committed":
                committed += 1
                if not crash_checked:
                    self._crash("after_window_commit", crash_at)
                    crash_checked = True
            else:
                already += 1
        return {"committed": committed, "already_committed": already}

    def dispatch(self, crash_at: str | None = None) -> dict[str, Any]:
        selected_crash = crash_at or os.environ.get("SCHED_CRASH_AT")
        with self.store.lock():
            if not self.store.schema_exists():
                fail("E_NO_STATE", "run load before dispatch")
            total_committed = 0
            total_already = 0
            total_prepared = 0
            while True:
                prepared_result = self._commit_prepared(selected_crash)
                total_committed += prepared_result["committed"]
                total_already += prepared_result["already_committed"]

                with self.store.tx() as conn:
                    model, precision, manifest, replicas = (
                        self.store.load_dataset_from_db(conn)
                    )
                    pending = self.store.pending_requests(conn)
                    if not pending:
                        break
                    candidates = self.store.candidate_map(
                        conn,
                        pending,
                        model,
                        replicas,
                        manifest.version,
                    )
                    next_starts = self.store.next_window_starts(
                        conn, manifest.version, replicas
                    )
                    planned, _ = plan_windows(
                        pending,
                        candidates,
                        replicas,
                        manifest.version,
                        next_starts,
                        model,
                        precision,
                    )
                    if not planned:
                        fail(
                            "E_SCHEDULER",
                            "pending requests produced no windows",
                        )

                for item in planned:
                    self.store.prepare_window(item.plan)
                    total_prepared += 1
                self._crash("after_prepare_windows", selected_crash)

            self._crash("after_all_commit", selected_crash)
            self._crash("after_commit_before_settle", selected_crash)
            horizon = self.store.active_completion_horizon()
            settled = self.store.settle_due(horizon)
            self._crash("after_settle", selected_crash)

            status = self.store.status(details=False)
            return {
                "ok": True,
                "action": "dispatch",
                "prepared_windows": total_prepared,
                "committed_windows": total_committed,
                "already_committed_windows": total_already,
                "settlement": settled,
                "status": {
                    "logical_clock_watermark_ms": status[
                        "logical_clock_watermark_ms"
                    ],
                    "request_counts": status["request_counts"],
                    "committed_windows_total": status[
                        "committed_windows_total"
                    ],
                    "committed_batches_total": status[
                        "committed_batches_total"
                    ],
                    "precision_sensitive_total": status[
                        "precision_sensitive_total"
                    ],
                },
            }

    def resume(self, crash_at: str | None = None) -> dict[str, Any]:
        result = self.dispatch(crash_at=crash_at)
        result["action"] = "resume"
        return result

    def record_failure(
        self, request_id: str, attempts: int = 1, error: str = "EXECUTION_ERROR"
    ) -> dict[str, Any]:
        return self.store.record_execution_failure(
            request_id, attempts=attempts, error=error
        )

    def status(self, details: bool = False) -> dict[str, Any]:
        return self.store.status(details=details)

    def verify(self) -> dict[str, Any]:
        return self.store.verify()

    def window_plan_hash(self, plan: dict[str, Any]) -> str:
        return plan_hash(plan)

