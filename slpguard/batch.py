"""Concurrent batch processing with deterministic aggregation."""

from __future__ import annotations

import hashlib
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .analyzer import analyze_path
from .checkpoint import (
    CheckpointState,
    CheckpointStore,
    aggregate_results,
)
from .errors import ErrorCode, SLPError
from .io_utils import atomic_write_json, canonical_json_bytes, cleanup_temp_files
from .models import FileResult, JudgeConfig, ResourceLimits, TruthSummary
from .protocol import CHECKPOINT_SCHEMA_VERSION, PHASE_TABLE_VERSION


@dataclass(frozen=True)
class BatchConfig:
    workers: int = 4
    limits: ResourceLimits = ResourceLimits()
    judge: JudgeConfig = JudgeConfig()
    resume: bool = True
    checkpoint_enabled: bool = True
    clean_temp: bool = True
    report_path: str | None = None
    checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION
    phase_table_version: int = PHASE_TABLE_VERSION

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be at least 1")

    def fingerprint(self) -> str:
        material = {
            "limits": self.limits.to_dict(),
            "judge": self.judge.to_dict(),
            "checkpoint_schema_version": self.checkpoint_schema_version,
            "phase_table_version": self.phase_table_version,
        }
        return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def discover_files(input_path: str | Path) -> list[Path]:
    target = Path(input_path)
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(path for path in target.rglob("*.slp") if path.is_file())
    raise SLPError(ErrorCode.E_ARG, f"input path does not exist: {target}")


def _error_result(path: Path, error: SLPError, limits: ResourceLimits) -> FileResult:
    return FileResult(
        path=str(path),
        file_id="",
        status="io_error" if error.code is ErrorCode.E_IO else "parse_error",
        version=None,
        declared_message_count=None,
        processed_messages=0,
        sessions=[],
        parse_error=error.to_dict(),
        termination=error.code.name,
        termination_detail=error.detail,
        truth=TruthSummary(),
        elapsed_seconds=0.0,
        steps_by_session={},
        limits=limits.to_dict(),
    )


def _safe_analyze(
    path: Path,
    config: BatchConfig,
    file_id: str,
) -> FileResult:
    try:
        result = analyze_path(path, limits=config.limits, judge_config=config.judge)
        if result.file_id != file_id:
            result.file_id = file_id
        return result
    except SLPError as error:
        result = _error_result(path, error, config.limits)
        result.file_id = file_id
        return result
    except Exception as error:  # isolate unexpected per-file failures
        result = _error_result(
            path,
            SLPError(ErrorCode.E_IO, f"unexpected per-file failure: {error}"),
            config.limits,
        )
        result.file_id = file_id
        return result


def _normalize_for_compare(result: dict[str, Any]) -> dict[str, Any]:
    copied = dict(result)
    copied.pop("elapsed_seconds", None)
    copied.pop("checkpoint_reused", None)
    return copied


def normalized_results(report: dict[str, Any]) -> list[dict[str, Any]]:
    results = [_normalize_for_compare(item) for item in report.get("files", [])]
    return sorted(results, key=lambda item: item.get("path", ""))


def run_batch(
    input_path: str | Path,
    *,
    config: BatchConfig | None = None,
    workdir: str | Path | None = None,
    on_file_complete: Callable[[FileResult], None] | None = None,
) -> dict[str, Any]:
    """Run a deterministic batch.

    Worker threads may finish in any order, but results are sorted by relative
    path and all checkpoint mutations occur on the coordinator thread under the
    process-safe checkpoint lock.
    """

    cfg = config or BatchConfig()
    root = Path(workdir or Path.cwd()).resolve()
    reports_dir = root / "reports"
    checkpoints_dir = root / "checkpoints"
    reports_dir.mkdir(parents=True, exist_ok=True)
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    cleaned = (
        cleanup_temp_files([reports_dir, checkpoints_dir])
        if cfg.clean_temp
        else []
    )
    files = discover_files(input_path)
    completion_ids: dict[Path, str] = {}
    relative_paths: dict[Path, str] = {}
    for path in files:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise SLPError(ErrorCode.E_IO, f"cannot read {path}: {error}") from error
        try:
            relative_paths[path] = str(path.resolve().relative_to(root))
        except ValueError:
            relative_paths[path] = str(path.resolve())
        completion_ids[path] = (
            f"{relative_paths[path]}:{hashlib.sha256(data).hexdigest()}"
        )

    store = (
        CheckpointStore(
            root,
            config_fingerprint=cfg.fingerprint(),
            checkpoint_schema_version=cfg.checkpoint_schema_version,
            phase_table_version=cfg.phase_table_version,
        )
        if cfg.checkpoint_enabled
        else None
    )
    checkpoint_errors: list[dict[str, Any]] = []
    checkpoint_state = CheckpointState(completed={})
    checkpoint_rebuilt = False
    checkpoint_rebuild_reason: str | None = None
    if store is not None:
        try:
            checkpoint_state = store.load()
            checkpoint_rebuilt = checkpoint_state.rebuilt
            checkpoint_rebuild_reason = checkpoint_state.rebuild_reason
        except SLPError as error:
            checkpoint_errors.append(error.to_dict())
            if error.code is ErrorCode.E_LOCK:
                # Do not attempt any write while another writer owns the lock;
                # the existing checkpoint must remain untouched.
                store = None

    completed: dict[str, FileResult] = {}
    pending: list[tuple[Path, str]] = []
    if cfg.resume:
        for path in files:
            file_id = completion_ids[path]
            stored = checkpoint_state.completed.get(file_id)
            if stored is None:
                pending.append((path, file_id))
                continue
            try:
                result = FileResult.from_dict(stored)
                result.path = relative_paths[path]
                result.checkpoint_reused = True
                completed[file_id] = result
            except (KeyError, TypeError, ValueError):
                pending.append((path, file_id))
    else:
        pending = [(path, completion_ids[path]) for path in files]

    def save_one(result: FileResult) -> None:
        if store is None:
            return
        try:
            store.record_file(result.file_id, result.to_dict())
        except SLPError as error:
            checkpoint_errors.append(error.to_dict())

    if pending:
        with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="slp") as pool:
            futures: dict[Future[FileResult], tuple[Path, str]] = {
                pool.submit(_safe_analyze, path, cfg, file_id): (path, file_id)
                for path, file_id in pending
            }
            for future in as_completed(futures):
                path, file_id = futures[future]
                result = future.result()
                result.path = relative_paths[path]
                completed[file_id] = result
                save_one(result)
                if on_file_complete is not None:
                    on_file_complete(result)

    path_by_id = {completion_ids[path]: path for path in files}
    ordered = sorted(
        completed.items(),
        key=lambda item: relative_paths[path_by_id[item[0]]],
    )
    result_dicts = [result.to_dict() for _, result in ordered]
    summary = aggregate_results(result_dicts)

    if store is not None:
        try:
            store.replace_current({file_id: result.to_dict() for file_id, result in ordered})
        except SLPError as error:
            checkpoint_errors.append(error.to_dict())

    report_payload: dict[str, Any] = {
        "schema": "slpguard-report-v1",
        "protocol": {
            "supported_versions": [1, 2],
            "phase_table_version": cfg.phase_table_version,
            "checkpoint_schema_version": cfg.checkpoint_schema_version,
        },
        "input": str(Path(input_path).resolve()),
        "batch": {
            **summary,
            "schedule_summary": {
                "files": summary["files"],
                "success": summary["success"],
                "limited": summary["limited"],
                "failed": summary["failed"],
            },
            "workers": cfg.workers,
            "checkpoint_enabled": cfg.checkpoint_enabled,
            "checkpoint_rebuilt": checkpoint_rebuilt,
            "checkpoint_rebuild_reason": checkpoint_rebuild_reason,
            "checkpoint_reused_files": sum(
                1 for result in completed.values() if result.checkpoint_reused
            ),
            "checkpoint_errors": checkpoint_errors,
            "temporary_files_removed": cleaned,
        },
        "rejections": [
            {
                "path": result["path"],
                "status": result["status"],
                "code": (
                    result.get("parse_error", {}).get("code")
                    if result.get("parse_error")
                    else result.get("termination")
                ),
                "processed_messages": result["processed_messages"],
                "declared_message_count": result["declared_message_count"],
                "termination": result["termination"],
            }
            for result in result_dicts
            if result["status"] != "ok"
        ],
        "files": result_dicts,
    }

    report_path = Path(cfg.report_path) if cfg.report_path else reports_dir / "slp-report.json"
    if not report_path.is_absolute():
        report_path = root / report_path
    atomic_write_json(report_path, report_payload, kind="slp-report")
    report_payload["report_path"] = str(report_path)
    return report_payload
