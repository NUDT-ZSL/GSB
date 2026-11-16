#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import Database  # noqa: E402
from app.reconciliation import build_markdown, compare_summaries  # noqa: E402

ARTIFACTS = ROOT / "artifacts" / "recovery"
COMMON = {
    "render_timeout_s": 4.0,
    "max_render_observations": 60,
    "max_retries": 4,
    "backoff_base_ms": 60,
    "backoff_max_ms": 600,
    "high_latency_ms": 500,
    "max_session_replays": 6,
    "request_timeout_s": 10,
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{base_url}/api/health", timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:  # pragma: no cover
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"server did not become healthy: {last_error}")


def start_server(db_path: Path, port: int, label: str) -> subprocess.Popen:
    log_path = ARTIFACTS / f"server-{label}-{port}.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(port),
            "HOST": "127.0.0.1",
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "AUDIT_DB_PATH": str(db_path),
            "LOG_LEVEL": "warning",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    wait_health(f"http://127.0.0.1:{port}")
    return process


def stop_server(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def task_payload(name: str, wave: str, *, concurrency: int, delay_profile: str) -> dict[str, Any]:
    return {
        "name": name,
        "wave": wave,
        "concurrency": concurrency,
        "delay_profile": delay_profile,
        "fault_profile": "mixed",
        **COMMON,
    }


def create_task(client: httpx.Client, payload: dict[str, Any]) -> str:
    response = client.post("/api/tasks", json=payload)
    response.raise_for_status()
    return response.json()["task_id"]


def wait_task(client: httpx.Client, task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/tasks/{task_id}").json()
        status = detail["task"]["status"]
        if status not in {"created", "running"}:
            if status != "completed":
                raise RuntimeError(
                    f"task {task_id} ended as {status}: "
                    f"{detail['task'].get('task_code')} {detail['task'].get('error')}"
                )
            return detail
        time.sleep(0.08)
    raise RuntimeError(f"task {task_id} did not finish in {timeout}s")


def record_revisions(summary: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for business in summary["businesses"]:
        for record in business["records"]:
            values.append(
                f"{business['business_id']}@{record['schema_version']}#{record['revision']}"
            )
    return sorted(values)


def run_reference(db_path: Path) -> dict[str, Any]:
    if db_path.exists():
        db_path.unlink()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = start_server(db_path, port, "reference")
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            baseline_id = create_task(
                client, task_payload("reference-baseline", "baseline", concurrency=3, delay_profile="slow")
            )
            wait_task(client, baseline_id)
            baseline_summary = Database(db_path).global_consistency_summary()
            incremental_id = create_task(
                client,
                task_payload("reference-incremental", "incremental", concurrency=3, delay_profile="slow"),
            )
            wait_task(client, incremental_id)
        final_summary = Database(db_path).global_consistency_summary()
        return {
            "db_path": str(db_path),
            "port": port,
            "baseline_task_id": baseline_id,
            "incremental_task_id": incremental_id,
            "baseline_summary": baseline_summary,
            "final_summary": final_summary,
        }
    finally:
        stop_server(process)


def run_crash_and_resume(db_path: Path) -> dict[str, Any]:
    if db_path.exists():
        db_path.unlink()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = start_server(db_path, port, "crash")
    baseline_id = ""
    before_kill: dict[str, Any] = {}
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            baseline_id = create_task(
                client,
                task_payload(
                    "crash-baseline",
                    "baseline",
                    concurrency=1,
                    delay_profile="bursty",
                ),
            )
        db = Database(db_path)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            progress = db.task_progress(baseline_id)
            total = progress["total_items"]
            completed = progress["completed_items"]
            if total > 0 and 0 < completed < total:
                task = db.get_task(baseline_id)
                checkpoint = db.get_checkpoint(baseline_id)
                records = db.records_for_task(baseline_id)
                summary = db.global_consistency_summary()
                before_kill = {
                    "task_status": task["status"],
                    "completed_items": completed,
                    "total_items": total,
                    "checkpoint": checkpoint,
                    "revision_sequence": sorted(
                        f"{record['business_id']}@{record['schema_version']}#{record['revision']}"
                        for record in records
                    ),
                    "edge_count": summary["edge_count"],
                    "record_count": summary["record_count"],
                }
                process.kill()
                process.wait(timeout=10)
                break
            time.sleep(0.008)
        if not before_kill:
            raise RuntimeError("could not catch the crawl mid-flight before it completed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    # The original process left no exit report.  Restarting with the same DB must be
    # enough for the explicit resume endpoint to continue.
    time.sleep(0.15)
    restarted = start_server(db_path, port, "restarted")
    try:
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            stale = client.get(f"/api/tasks/{baseline_id}").json()
            resume = client.post(f"/api/tasks/{baseline_id}/resume")
            resume.raise_for_status()
            resumed = wait_task(client, baseline_id)
            after_resume_summary = Database(db_path).global_consistency_summary()
            incremental_id = create_task(
                client,
                task_payload(
                    "crash-incremental",
                    "incremental",
                    concurrency=4,
                    delay_profile="bursty",
                ),
            )
            wait_task(client, incremental_id)
        final_summary = Database(db_path).global_consistency_summary()
        after = {
            "task_status": resumed["task"]["status"],
            "completed_items": resumed["progress"]["completed_items"],
            "total_items": resumed["progress"]["total_items"],
            "checkpoint": resumed["checkpoint"],
            "revision_sequence": record_revisions(after_resume_summary),
            "edge_count": after_resume_summary["edge_count"],
            "record_count": after_resume_summary["record_count"],
            "final_record_count": final_summary["record_count"],
            "final_edge_count": final_summary["edge_count"],
        }
        return {
            "db_path": str(db_path),
            "port": port,
            "baseline_task_id": baseline_id,
            "incremental_task_id": incremental_id,
            "stale_status_seen_after_restart": stale["task"]["status"],
            "before_kill": before_kill,
            "after_resume": after,
            "baseline_summary_after_resume": after_resume_summary,
            "final_summary": final_summary,
        }
    finally:
        stop_server(restarted)


def build_recovery_markdown(report: dict[str, Any]) -> str:
    before = report["crash_run"]["before_kill"]
    after = report["crash_run"]["after_resume"]
    lines = [
        "# 强制终止与检查点恢复验证",
        "",
        f"- 结论：**{'PASS' if report['identical_to_control'] and report['invariants_ok'] else 'FAIL'}**",
        f"- 终止前任务状态：`{before['task_status']}`（无退出报告）",
        f"- 重启后库中状态：`{report['crash_run']['stale_status_seen_after_restart']}`",
        f"- 终止前已确认条目：{before['completed_items']} / {before['total_items']}",
        f"- 恢复后已确认条目：{after['completed_items']} / {after['total_items']}",
        f"- 终止前修订序列：{', '.join(before['revision_sequence']) or '∅'}",
        f"- 恢复后修订序列：{', '.join(after['revision_sequence']) or '∅'}",
        f"- 终止前关联边数：{before['edge_count']}；恢复后（基线阶段）：{after['edge_count']}；最终：{after['final_edge_count']}",
        f"- 恢复结果与未中断对照运行逐业务一致：**{'是' if report['identical_to_control'] else '否'}**",
        "",
        "恢复仅读取同一 SQLite 文件中的任务配置、分页游标、已完成条目键与会话记录；"
        "重启后的站点内存会话已丢失，采集器重新获取会话并重放当前请求。",
        "",
    ]
    comparison = report["control_comparison"]
    lines.extend(
        [
            f"对账：业务 {comparison['business_count']} 个，规范化记录 "
            f"{comparison['record_count']['left']}/{comparison['record_count']['right']}，"
            f"关联边 {comparison['edge_count']['left']}/{comparison['edge_count']['right']}。",
            "",
            "| 业务标识 | 记录（恢复/对照） | 边（恢复/对照） | 结论 |",
            "|---|---:|---:|---|",
        ]
    )
    for item in comparison["comparisons"]:
        lines.append(
            f"| `{item['business_id']}` | {item['right_records']}/{item['left_records']} | "
            f"{item['right_edges']}/{item['left_edges']} | {'一致' if item['identical'] else '差异'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    reference = run_reference(ARTIFACTS / "reference.sqlite3")
    crash = run_crash_and_resume(ARTIFACTS / "crash.sqlite3")
    control_comparison = compare_summaries(
        reference["final_summary"], crash["final_summary"]
    )
    baseline_comparison = compare_summaries(
        reference["baseline_summary"], crash["baseline_summary_after_resume"]
    )
    before = crash["before_kill"]
    after = crash["after_resume"]
    before_revisions = set(before["revision_sequence"])
    after_revisions = set(after["revision_sequence"])
    invariants = {
        "completed_count_not_decreased": after["completed_items"] >= before["completed_items"],
        "confirmed_revision_subset_preserved": before_revisions <= after_revisions,
        "edge_count_not_decreased": after["edge_count"] >= before["edge_count"],
        "checkpoint_has_cursor": bool(after["checkpoint"]["cursor"]),
        "stale_running_task_was_resumable": crash["stale_status_seen_after_restart"] == "running",
        "baseline_resume_matches_control": baseline_comparison["identical"],
    }
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reference": {
            key: value
            for key, value in reference.items()
            if key not in {"baseline_summary", "final_summary"}
        },
        "crash_run": {
            key: value
            for key, value in crash.items()
            if key not in {"baseline_summary_after_resume", "final_summary"}
        },
        "control_comparison": {
            key: value for key, value in control_comparison.items() if key not in {"left", "right"}
        },
        "baseline_comparison": {
            key: value for key, value in baseline_comparison.items() if key not in {"left", "right"}
        },
        "invariants": invariants,
        "invariants_ok": all(invariants.values()),
        "identical_to_control": control_comparison["identical"],
    }
    report_path = ARTIFACTS / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = build_recovery_markdown(report)
    (ARTIFACTS / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"artifacts: {report_path}")
    return 0 if report["invariants_ok"] and report["identical_to_control"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
