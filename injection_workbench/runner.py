"""批处理编排：预算控制、并发执行、检查点续接与报告汇总。"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

from .analyzer import analyze_manifest
from .checkpoints import CheckpointStore
from .config import Config
from .parser import load_manifest
from .resources import (
    BudgetExceeded,
    Deadline,
    DeadlineExceeded,
    LockConflict,
    atomic_write_text,
    file_lock,
)


def _error_entry(manifest_path: Path, code: str, message: str, elapsed: float,
                 file_id: str | None = None) -> dict:
    return {
        "manifest": str(manifest_path),
        "file_id": file_id,
        "status": "failed",
        "error_code": code,
        "message": message,
        "elapsed_seconds": round(elapsed, 6),
    }


def run_batch(manifest_paths: list[str | Path], config: Config,
              checkpoint_store: CheckpointStore | None = None,
              report_path: str | Path | None = None,
              force_mode: str | None = None) -> dict:
    """并发处理一批清单，单份失败被隔离并计入报告，支持检查点续接。"""
    state = checkpoint_store.load() if checkpoint_store else None
    completed: dict[str, dict] = dict(state.completed_files) if state else {}
    lock_error: dict | None = None

    # 检查点命中的清单直接复用，未完成者进入执行队列
    pending: list[Path] = []
    results: list[dict] = []
    file_id_by_path: dict[str, str] = {}
    for item in manifest_paths:
        path = Path(item)
        candidate_key = str(path)
        hit = completed.get(candidate_key)
        if hit is None:
            pending.append(path)
        else:
            results.append({
                "manifest": candidate_key,
                "file_id": hit.get("file_id"),
                "status": "ok",
                "from_checkpoint": True,
                "mode": hit.get("mode"),
                "sink_verdicts": hit.get("sink_verdicts", []),
            })

    def _job(path: Path) -> dict:
        deadline = Deadline(config.budget.deadline_seconds)
        start = time.perf_counter()
        try:
            return process_manifest(path, config, deadline=deadline, force_mode=force_mode)
        except DeadlineExceeded as exc:
            return _error_entry(path, exc.error_code, str(exc), time.perf_counter() - start)
        except BudgetExceeded as exc:
            return _error_entry(path, exc.error_code, str(exc), time.perf_counter() - start)
        except Exception as exc:  # 单份清单的任何异常都不得中断同批
            return _error_entry(path, "E_INTERNAL", f"{type(exc).__name__}: {exc}",
                                time.perf_counter() - start)

    if pending:
        workers = max(1, min(config.budget.concurrency, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_job, path): path for path in pending}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                if result.get("status") == "ok":
                    completed[result["manifest"]] = {
                        "file_id": result.get("file_id"),
                        **_sink_summary(result),
                    }

    # 检查点与报告均在文件锁内原子替换；锁冲突返回 E_LOCK 且不丢分析结果
    if checkpoint_store is not None:
        try:
            checkpoint_store.save(completed)
        except LockConflict as exc:
            lock_error = {"error_code": exc.error_code, "message": str(exc), "target": "checkpoint"}

    failed = [item for item in results if item.get("status") in ("failed", "rejected")]
    report = {
        "schema": "injection-workbench-report/1",
        "config": config.to_dict(),
        "checkpoint": {
            "enabled": checkpoint_store is not None,
            "rebuilt": state.rebuilt if state else False,
            "rebuild_reason": state.rebuild_reason if state else None,
            "resumed_files": len(results) - len(pending),
        },
        "summary": {
            "total_manifests": len(manifest_paths),
            "ok": sum(1 for item in results if item.get("status") == "ok"),
            "failed": len(failed),
            "lock_error": lock_error,
        },
        "manifests": results,
    }

    if report_path is not None:
        target = Path(report_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with file_lock(str(target.parent / ".report.lock")):
                atomic_write_text(str(target), json.dumps(report, ensure_ascii=False, indent=2))
        except LockConflict as exc:
            report["summary"]["lock_error"] = {
                "error_code": exc.error_code, "message": str(exc), "target": "report"
            }

    return report


def _sink_summary(result: dict) -> dict:
    """检查点中持久化的汇点聚合结论（精简形态）。"""
    return {
        "dialect": result["dialect"],
        "mode": result["mode"],
        "sink_verdicts": [
            {
                "sink_id": item["sink_id"],
                "verdict": item["verdict"],
                "unique_defect_count": item["unique_defect_count"],
                "duplicate_count": item["duplicate_count"],
            }
            for item in result["sink_verdicts"]
        ],
    }


def process_manifest(manifest_path: str | Path, config: Config,
                     deadline: Deadline | None = None,
                     force_mode: str | None = None) -> dict:
    """处理单份清单，应用真实资源预算。

    路径数超限在分析前判定 E_BUDGET；耗时以真实墙钟在分析循环中
    协作式检查，超时抛 E_TIMEOUT。异常由调用方隔离，不影响同批其他清单。
    """
    manifest_path = Path(manifest_path)
    parsed, fatal = load_manifest(manifest_path)
    if fatal is not None:
        return {
            "manifest": str(manifest_path),
            "file_id": None,
            "status": "rejected",
            "error_code": fatal.error_code,
            "message": fatal.message,
            "elapsed_seconds": 0.0,
        }

    path_count = len(parsed.paths)
    if path_count > config.budget.max_paths_per_manifest:
        raise BudgetExceeded(config.budget.max_paths_per_manifest, path_count)

    active_deadline = deadline or Deadline(config.budget.deadline_seconds)
    start = time.perf_counter()
    result = analyze_manifest(parsed, config, force_mode=force_mode, deadline=active_deadline)
    result["manifest"] = str(manifest_path)
    result["status"] = "ok"
    result["elapsed_seconds"] = round(time.perf_counter() - start, 6)
    result["budget"] = {
        "deadline_seconds": config.budget.deadline_seconds,
        "max_paths_per_manifest": config.budget.max_paths_per_manifest,
    }
    return result
