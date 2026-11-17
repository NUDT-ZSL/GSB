from __future__ import annotations

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import command_merge, validate_config
from .errors import WorkbenchError
from .pipeline import RenderResult, Renderer
from .util import now_iso, read_json


@dataclass
class InteractiveJob:
    job_id: int
    config: dict[str, Any]
    config_fingerprint: str
    state: str = "queued"
    result: RenderResult | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    submitted_at: str = field(default_factory=now_iso)
    started_at: str | None = None
    finished_at: str | None = None


class InteractiveController:
    """Single-background-worker interactive command controller.

    The response path only waits for the configured preview budget. A render that
    exceeds the budget remains queued/running and is never cancelled by the
    budget timeout.
    """

    def __init__(
        self,
        base_config: Mapping[str, Any],
        *,
        base_dir: str | Path | None = None,
        renderer: Renderer | None = None,
        on_response: Callable[[dict[str, Any]], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else Path.cwd().resolve()
        self.current_config = copy.deepcopy(dict(base_config))
        self.renderer = renderer or Renderer(base_dir=self.base_dir)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wb-interactive")
        self._condition = threading.Condition(threading.RLock())
        self._next_job_id = 1
        self._jobs: dict[int, InteractiveJob] = {}
        self._futures: dict[int, Any] = {}
        self._latest_preview: dict[str, Any] | None = self._load_latest_preview()
        self._latest_progress: dict[str, Any] = {}
        self._on_response = on_response
        self.stop_event = stop_event or threading.Event()

    def submit(self, command: Mapping[str, Any], *, preview_budget_ms: int | None = None) -> dict[str, Any]:
        entered = time.monotonic()
        updates = command.get("set", command)
        if not isinstance(updates, Mapping):
            return {
                "job_id": None,
                "business_code": "E_CONFIG_INVALID",
                "message": "command set must be an object",
                "returned_at": now_iso(),
            }
        candidate = command_merge(self.current_config, updates)
        try:
            validated = validate_config(candidate, self.base_dir)
        except WorkbenchError as exc:
            response = self._response_payload(
                job=None,
                code=exc.code,
                budget_ms=int(preview_budget_ms if preview_budget_ms is not None else command.get("preview_budget_ms", 0)),
                entered=entered,
                message=exc.message,
                fresh=False,
            )
            self._emit(response)
            return response
        self.current_config = candidate
        config_fp = validated.config_fingerprint
        budget_ms = int(
            preview_budget_ms
            if preview_budget_ms is not None
            else command.get("preview_budget_ms", validated.preview_budget_ms)
        )
        if budget_ms < 0:
            budget_ms = 0
        with self._condition:
            job_id = self._next_job_id
            self._next_job_id += 1
            job = InteractiveJob(job_id=job_id, config=candidate, config_fingerprint=config_fp)
            self._jobs[job_id] = job
            self._futures[job_id] = self._executor.submit(self._run_job, job)
            self._condition.notify_all()

        safety_margin = min(10.0, max(4.0, budget_ms * 0.5))
        deadline = entered + max(0.0, budget_ms - safety_margin) / 1000.0
        with self._condition:
            while True:
                fresh = self._latest_preview is not None and self._latest_preview.get("config_fingerprint") == config_fp
                if fresh:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            fresh = self._latest_preview is not None and self._latest_preview.get("config_fingerprint") == config_fp
            preview = copy.deepcopy(self._latest_preview)
            stale = preview is not None and preview.get("config_fingerprint") != config_fp
            job_snapshot = self._jobs[job_id]
            progress = dict(job_snapshot.progress or self._latest_progress)
            state = job_snapshot.state
            progress["requested_job_id"] = job_id
            progress["job_state"] = state
            progress["pending_jobs"] = sum(1 for item in self._jobs.values() if item.state in {"queued", "running"})
        code = "OK" if fresh else "E_PREVIEW_BUDGET_PENDING"
        message = (
            "latest parameter preview is ready"
            if fresh
            else "preview budget elapsed; background render continues and the task was not discarded"
        )
        response = self._response_payload(
            job=job_snapshot,
            code=code,
            budget_ms=budget_ms,
            entered=entered,
            message=message,
            fresh=fresh,
            preview=preview,
            stale=bool(stale),
            progress=progress,
            state=state,
        )
        self._emit(response)
        return response

    def wait_job(self, job_id: int, timeout: float | None = None) -> RenderResult:
        with self._condition:
            future = self._futures.get(job_id)
        if future is None:
            raise KeyError(f"unknown job {job_id}")
        future.result(timeout=timeout)
        with self._condition:
            job = self._jobs[job_id]
            if job.result is None:
                raise RuntimeError(f"job {job_id} finished without a render result")
            return job.result

    def wait_all(self, timeout: float | None = None) -> list[RenderResult]:
        with self._condition:
            futures = list(self._futures.items())
        for _, future in futures:
            future.result(timeout=timeout)
        with self._condition:
            return [self._jobs[job_id].result for job_id, _ in futures if self._jobs[job_id].result is not None]

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "generated_at": now_iso(),
                "latest_preview": copy.deepcopy(self._latest_preview),
                "latest_progress": copy.deepcopy(self._latest_progress),
                "jobs": [
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "config_fingerprint": job.config_fingerprint,
                        "submitted_at": job.submitted_at,
                        "started_at": job.started_at,
                        "finished_at": job.finished_at,
                        "result": None
                        if job.result is None
                        else {
                            "status": job.result.status,
                            "business_codes": job.result.business_codes,
                            "snapshot_version": job.result.snapshot_version,
                            "report_path": job.result.report_path,
                        },
                        "error": job.error,
                    }
                    for job in self._jobs.values()
                ],
            }

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    # ------------------------------------------------------------------ worker
    def _run_job(self, job: InteractiveJob) -> None:
        with self._condition:
            job.state = "running"
            job.started_at = now_iso()
            self._condition.notify_all()

        def on_preview(preview: dict[str, Any]) -> None:
            with self._condition:
                self._latest_preview = copy.deepcopy(preview)
                job.progress = dict(job.progress)
                job.progress["preview_ready"] = True
                job.progress["preview_config_fingerprint"] = preview.get("config_fingerprint")
                self._condition.notify_all()

        def on_progress(progress: dict[str, Any]) -> None:
            with self._condition:
                progress = dict(progress)
                progress["job_id"] = job.job_id
                self._latest_progress = progress
                job.progress = progress
                self._condition.notify_all()

        try:
            result = self.renderer.render(
                job.config,
                allow_reuse=True,
                on_preview=on_preview,
                on_progress=on_progress,
                stop_event=self.stop_event,
            )
            with self._condition:
                job.result = result
                job.state = "completed" if result.status == "completed" else result.status
                job.finished_at = now_iso()
                job.progress = {
                    **dict(job.progress),
                    "status": result.status,
                    "snapshot_version": result.snapshot_version,
                    "business_codes": result.business_codes,
                    "report_path": result.report_path,
                }
                self._latest_progress = dict(job.progress)
                if result.preview is not None:
                    self._latest_preview = copy.deepcopy(result.preview)
                self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                job.state = "failed"
                job.error = {"type": type(exc).__name__, "message": str(exc)}
                job.finished_at = now_iso()
                self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()

    # ------------------------------------------------------------------ helpers
    def _response_payload(
        self,
        *,
        job: InteractiveJob | None,
        code: str,
        budget_ms: int,
        entered: float,
        message: str,
        fresh: bool,
        preview: dict[str, Any] | None = None,
        stale: bool = False,
        progress: Mapping[str, Any] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        if preview is None:
            with self._condition:
                preview = copy.deepcopy(self._latest_preview)
                if preview is not None:
                    stale = preview.get("config_fingerprint") != (None if job is None else job.config_fingerprint)
        elapsed_ms = (time.monotonic() - entered) * 1000.0
        return {
            "response_type": "preview",
            "job_id": None if job is None else job.job_id,
            "business_code": code,
            "message": message,
            "preview_budget_ms": budget_ms,
            "response_latency_ms": round(elapsed_ms, 6),
            "preview_fresh": bool(fresh),
            "preview_stale": bool(stale),
            "preview": preview,
            "progress": dict(progress or {}),
            "background": {
                "continues": code == "E_PREVIEW_BUDGET_PENDING",
                "render_terminated": False,
                "discarded": False,
                "state": state,
            },
            "returned_at": now_iso(),
        }

    def _load_latest_preview(self) -> dict[str, Any] | None:
        try:
            validated = validate_config(self.current_config, self.base_dir)
        except WorkbenchError:
            raw_output = self.current_config.get("output_dir")
            if not isinstance(raw_output, str):
                return None
            output_dir = Path(raw_output).expanduser()
            if not output_dir.is_absolute():
                output_dir = self.base_dir / output_dir
            return _scan_preview(output_dir.resolve())
        return _scan_preview(validated.output_dir)

    def _emit(self, response: dict[str, Any]) -> None:
        if self._on_response is not None:
            try:
                self._on_response(response)
            except Exception:
                pass


def _scan_preview(output_dir: Path) -> dict[str, Any] | None:
    preview_dir = output_dir / "previews"
    if not preview_dir.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for metadata_path in preview_dir.glob("preview_v*.json"):
        try:
            version = int(metadata_path.stem.split("_v", 1)[1])
        except (IndexError, ValueError):
            continue
        candidates.append((version, metadata_path))
    for _, metadata_path in sorted(candidates, reverse=True):
        try:
            metadata = read_json(metadata_path)
        except (OSError, json.JSONDecodeError):
            continue
        image_path = metadata_path.with_suffix(".png")
        if not image_path.exists():
            continue
        metadata["path"] = str(image_path)
        metadata["metadata_path"] = str(metadata_path)
        return metadata
    return None
