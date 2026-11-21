from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .analysis import build_report, persist_analysis
from .clock import Schedule, UnitPlan, build_schedule
from .errors import BizError
from .scene import Scene
from .stats import Observation, aggregate_gradient_summary, build_window_summary, decide_gradient
from .store import Store, new_owner_token
from .transport import LoopbackHTTPTransport, TransportResult


class Engine:
    def __init__(
        self,
        scene: Scene,
        db_path: Path,
        transport: LoopbackHTTPTransport | None = None,
        crash_after_committed_windows: int | None = None,
        busy_timeout_seconds: float = 120.0,
    ) -> None:
        self.scene = scene
        self.store = Store(Path(db_path))
        self.schedule = build_schedule(scene)
        self.transport = transport or LoopbackHTTPTransport()
        self.crash_after_committed_windows = crash_after_committed_windows
        self.busy_timeout_seconds = busy_timeout_seconds
        self._units_by_key = {unit.unit_key: unit for unit in self.schedule.units}
        self._committed_this_process = 0

    def run(self, resume_only: bool = False) -> dict[str, Any]:
        if resume_only and not self.store.exists():
            raise BizError(
                "E_DB_NOT_FOUND",
                "cannot resume because the database does not exist",
                details={"path": str(self.store.db_path)},
            )
        init = self.store.initialize(self.scene, self.schedule)
        self.store.reclaim_stale(os.getpid())
        owner_token = new_owner_token()
        owner_pid = os.getpid()
        busy_started = time.monotonic()

        while True:
            claim = self.store.claim_next_unit(self.scene, owner_pid, owner_token)
            state = claim["state"]
            if state == "claimed":
                unit_row = claim["unit"]
                plan = self._units_by_key.get(str(unit_row["unit_key"]))
                if plan is None:
                    self.store.abandon_unit(
                        str(unit_row["unit_key"]),
                        owner_token,
                        "UNIT_NOT_IN_SCHEDULE",
                    )
                    continue
                try:
                    self._process_unit(plan, owner_token)
                    busy_started = time.monotonic()
                except BaseException:
                    try:
                        self.store.release_unit(plan.unit_key, owner_token, "INTERRUPTED_BEFORE_COMMIT")
                    except Exception:
                        pass
                    raise
                continue
            if state in {"done", "budget_exhausted", "blocked"}:
                break
            if state == "busy":
                if time.monotonic() - busy_started > self.busy_timeout_seconds:
                    raise BizError(
                        "E_INCOMPLETE_RUN",
                        "timed out waiting for other evaluator processes",
                        details={"busy_timeout_seconds": self.busy_timeout_seconds},
                    )
                time.sleep(0.02)
                continue
            raise BizError("E_INTERNAL", f"unknown scheduler state: {state}")

        report = build_report(self.store)
        persist_analysis(self.store, report)
        return {
            "ok": True,
            "initialized": init["initialized"],
            "resumed": init["resumed"],
            "units_planned": len(self.schedule.units),
            "requests_planned": self.schedule.total_requests,
            "windows_committed_this_process": self._committed_this_process,
            "budget_used_ms": report["budget"]["budget_used_ms"],
            "capacity_conclusions": report["capacity_conclusions"],
            "report_sha256": report["report_sha256"],
            "report": report,
        }

    def _process_unit(self, plan: UnitPlan, owner_token: str) -> None:
        committed = self.store.committed_window_keys(plan.unit_key)
        tenant = self.scene.tenants[plan.tenant]
        for window in plan.windows:
            if window.window_key in committed:
                continue
            observations = [
                self._execute_request(request)
                for request in window.requests
            ]
            summary, payload, entries = build_window_summary(
                window,
                observations,
                self.scene.precision,
            )
            summary.update(
                {
                    "batch_id": plan.batch_id,
                    "service": plan.service,
                    "tenant": plan.tenant,
                    "gradient_index": plan.gradient_index,
                }
            )
            result = self.store.commit_window(
                plan.unit_key,
                summary,
                payload,
                entries,
                version=1,
            )
            if result["committed"]:
                self._committed_this_process += 1
                if (
                    self.crash_after_committed_windows is not None
                    and self._committed_this_process >= self.crash_after_committed_windows
                ):
                    os._exit(75)

        rows = self.store.load_windows_for_unit(plan.unit_key)
        summaries = [json.loads(row["summary_json"]) for row in rows]
        payloads = [json.loads(row["payload_json"]) for row in rows]
        metric, ledger_entries = aggregate_gradient_summary(
            plan.unit_key,
            summaries,
            payloads,
            self.scene.precision,
        )
        decision = decide_gradient(
            metric,
            tenant.success_rate_min,
            tenant.p95_latency_max_ms,
            self.scene.precision,
        )
        self.store.complete_unit(
            plan.unit_key,
            owner_token,
            metric,
            decision,
            ledger_entries,
            next(batch for batch in self.scene.batches if batch.batch_id == plan.batch_id),
        )

    def _execute_request(self, request) -> Observation:
        service = self.scene.services[request.service]
        tenant = self.scene.tenants[request.tenant]
        result: TransportResult = self.transport.execute(
            service,
            tenant,
            request,
            self.scene.precision,
        )
        return Observation(
            request_id=request.request_id,
            request_index=request.request_index,
            wave_index=request.wave_index,
            release_ms=request.release_ms,
            window_index=request.window_index,
            http_status=result.http_status,
            success=result.success,
            latency_ms=result.latency_ms,
            error_code=result.error_code,
            error_message=result.error_message,
            error_fields=result.error_fields,
            raw_body=result.raw_body,
        )

